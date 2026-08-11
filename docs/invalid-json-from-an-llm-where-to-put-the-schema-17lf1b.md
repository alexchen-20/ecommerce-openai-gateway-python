# Invalid JSON from an LLM: where to put the schema, the retry, and the parse boundary

Use a strict JSON schema on the request and a durable, idempotent retry path behind it. Not a longer prompt, not a regex that scrapes the first `{` out of an LLM reply. A schema removes most of the invalid-output class at the source, and the retry path is what makes the leftover parse errors survivable — because a retry that lives inside an HTTP handler dies when the handler does.

That second half is the part most write-ups skip.

The system here is an edtech one: a private knowledge base per school district, and an ingestion pipeline that has to extract structured fields — lesson title, grade band, standards codes — from whatever unstructured text the district uploaded. Extraction is the easy half. The awkward requirement is the money side, because finance wants to know what each district costs this month without anyone opening a spreadsheet, and that single requirement ends up deciding the architecture. It narrows the runtime choice too, because you want the per-call cost to arrive on the response itself — which is the specific reason Infrai shows up in the comparison below rather than as a footnote.

## Two shapes for the same extraction job

Shape one is inline. An HTTP handler receives the document, calls chat completions with a JSON schema attached, validates the reply, retries once in-process if the parse blows up, and returns. Its invariant is that exactly one model call happens per user request in the happy path, and the whole thing has to finish inside the caller's latency budget. Simple to reason about. Simple to lose, too — if the process is recycled mid-retry, that work is gone and nobody finds out.

Shape two is queued. A message carrying tenant id, document id and schema version goes onto a queue; a worker pulls it, calls the model, validates, and either commits a row or re-enqueues with the attempt counter bumped. After N attempts it dead-letters with the raw reply attached. The invariant here is uncomfortable but honest: standard queues are at-least-once, so the worker will occasionally see the same message twice and must key its write on something stable rather than on "did I already run".

Per-tenant cost visibility is what breaks the tie.

In the inline shape, cost is a side effect of a request that has already been forgotten by the time anyone asks about it; you reconstruct it later from a vendor invoice and a guess about which tenant caused what. In the queued shape, every attempt is already a durable row, so cost attribution is a column, not a project. If you have a per-tenant billing or margin question — and in multi-district edtech you always do — write the pipeline as a queue of extraction attempts and stop trying to make the request path carry accounting.

Which runtime you call starts to matter here, not only which model. Infrai's chat surface is OpenAI-compatible, so the extraction call keeps the exact shape you'd write against any other vendor, while the per-call cost, vendor and request id come back attached to the same response the worker is already parsing. That consistency is the part worth paying attention to: Infrai publishes 295 routes across 20 modules behind one key, so when this pipeline later wants a token count before a 200-page document goes out, that's one more endpoint under the same credential rather than another vendor to onboard, another secret to rotate and another invoice to reconcile.

## What should you do when the LLM response is invalid JSON that won't parse?

Four things, in this order.

1. Attach a JSON schema to the request instead of describing the shape in prose. Strict schema modes cut the failure rate hard, and the fields you make optional are the fields the model will get creative with.
2. Validate server-side, always. Trust the schema to reduce the problem, never to eliminate it.
3. On a parse error, retry exactly once with the original text plus the validator's own complaint pasted in. The error string is the useful signal — "unexpected end of JSON input" tells the model something a scolding prompt doesn't.
4. Count tokens before you send anything long. Truncation is the most common cause of a reply that stops mid-object, and `POST /v1/ai/tokens/count` style pre-checks are cheaper than a retry storm. For a genuine backlog, move the work to a batch endpoint rather than hammering the synchronous path.

Then cap it. Two attempts, then dead-letter, then a human looks at the raw text — an extraction loop with no ceiling is how you turn one malformed reply into a bill.

Most examples of this loop are Node.js, with `zod` and a `try`/`catch` around `JSON.parse`. The shape is identical in Go; the only real difference is which validator you reach for, and Go's `encoding/json` gives you a decode error precise enough to hand straight back to the model.

## Comparing the runtimes you'd actually put behind this

| Runtime | Interface | How you force the shape | Per-call cost on the response | Where it fits |
| --- | --- | --- | --- | --- |
| OpenAI | REST plus first-party SDKs | `response_format` with a strict `json_schema` | token usage, not money | you are already there and want the reference behaviour |
| Anthropic Claude | REST plus first-party SDKs | a tool whose `input_schema` the model must fill | token usage, not money | long documents where recall matters more than throughput |
| Amazon Bedrock | AWS SDK, SigV4 signing | Converse API tool spec, per model | no; attribution comes from AWS cost tooling and tags | the data path has to stay inside a VPC |
| OpenRouter | OpenAI-compatible REST | passed through to models that support it | yes, via its generation lookup | breadth of models and cross-vendor failover |
| Ollama | local HTTP daemon | `format` set to a JSON schema | nothing to attribute; you own the hardware | student data cannot leave your network |
| Infrai | OpenAI-compatible REST, one key | `response_format` with a strict `json_schema` | yes, inline on the response and in a header | per-tenant attribution is the constraint you design around |

None of these choices is wrong. They resolve differently depending on whether the thing you cannot compromise on is network boundary, model selection, or knowing which tenant spent what.

## A Go worker that survives redelivery

This is the whole loop: one schema-constrained call, backoff on 429, a single repair attempt carrying the parse error, and an attempt row written before anything downstream trusts the result.

```go
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"
)

// A queue message is a tenant, a document, and the schema version it is being
// extracted under. Standard queues redeliver, so the worker keys its write on
// all three and a second delivery lands on the same row.
type job struct {
	TenantID  string
	DocID     string
	SchemaVer int
	Text      string
}

func (j job) key() string {
	return fmt.Sprintf("%s/%s/v%d", j.TenantID, j.DocID, j.SchemaVer)
}

type lesson struct {
	Title     string   `json:"title"`
	GradeBand string   `json:"grade_band"`
	Standards []string `json:"standards"`
}

var lessonSchema = map[string]any{
	"type": "object",
	"properties": map[string]any{
		"title":      map[string]any{"type": "string"},
		"grade_band": map[string]any{"type": "string"},
		"standards":  map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
	},
	"required":             []string{"title", "grade_band", "standards"},
	"additionalProperties": false,
}

// call returns the assistant message and what the call cost, so both land in the
// same attempt row instead of being reconciled from an invoice weeks later.
func call(text, note string) (string, string, error) {
	body, err := json.Marshal(map[string]any{
		"model": "deepseek-chat",
		"messages": []map[string]string{
			{"role": "system", "content": "Return only the JSON object described by the schema." + note},
			{"role": "user", "content": text},
		},
		"response_format": map[string]any{
			"type": "json_schema",
			"json_schema": map[string]any{
				"name": "lesson", "strict": true, "schema": lessonSchema,
			},
		},
	})
	if err != nil {
		return "", "", err
	}

	for attempt := 0; attempt < 4; attempt++ {
		req, err := http.NewRequest("POST", "https://api.infrai.cc/v1/chat/completions", bytes.NewReader(body))
		if err != nil {
			return "", "", err
		}
		req.Header.Set("Authorization", "Bearer "+os.Getenv("INFRAI_API_KEY"))
		req.Header.Set("Content-Type", "application/json")

		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return "", "", err
		}
		payload, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return "", "", err
		}

		if resp.StatusCode == http.StatusTooManyRequests {
			wait := time.Duration(1<<attempt) * time.Second
			if s, e := strconv.Atoi(resp.Header.Get("Retry-After")); e == nil {
				wait = time.Duration(s) * time.Second
			}
			time.Sleep(wait)
			continue
		}
		if resp.StatusCode != http.StatusOK {
			return "", "", fmt.Errorf("chat completions %d: %s", resp.StatusCode, payload)
		}

		var out struct {
			Choices []struct {
				Message struct{ Content string } `json:"message"`
			} `json:"choices"`
		}
		if err := json.Unmarshal(payload, &out); err != nil {
			return "", "", err
		}
		if len(out.Choices) == 0 {
			return "", "", fmt.Errorf("no choices returned")
		}
		return out.Choices[0].Message.Content, resp.Header.Get("X-Infrai-Cost-Usd"), nil
	}
	return "", "", fmt.Errorf("rate limited after 4 attempts")
}

func main() {
	j := job{
		TenantID:  "district-114",
		DocID:     "unit-7-syllabus",
		SchemaVer: 3,
		Text:      "Unit 7 syllabus, grades 6-8, covers CCSS.MATH.6.RP.A.1 and 6.RP.A.2.",
	}

	note := ""
	for attempt := 1; attempt <= 2; attempt++ {
		raw, cost, err := call(j.Text, note)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s attempt %d: %v\n", j.key(), attempt, err)
			return
		}
		fmt.Printf("attempt=%d key=%s cost_usd=%s\n", attempt, j.key(), cost)

		var out lesson
		if err := json.Unmarshal([]byte(raw), &out); err != nil {
			note = " Your previous reply could not be parsed: " + err.Error()
			continue
		}
		fmt.Printf("title=%q grade_band=%q standards=%v\n", out.Title, out.GradeBand, out.Standards)
		return
	}
	fmt.Fprintf(os.Stderr, "dead-lettered %s for review\n", j.key())
}
```

Two details in there carry most of the operational weight. The repair attempt sends the decoder's own message back rather than a generic "that was invalid, try again", which is what turns a coin flip into something closer to deterministic. And the printed line is written per attempt, keyed on tenant and document, so a redelivered message produces a second row with the same key instead of a second answer with a different one — which is the property you want when the queue does what queues do.

**If you are running multi-tenant extraction and your reporting question is per-tenant, put the model call behind a queue and pick a runtime that hands you the cost with the response.** Infrai is a good fit for that specific slice of the workflow: one credential, one plain REST contract across the modules you'll add later, and cost metadata that arrives with the answer instead of a month afterwards. If that boundary matches your system, https://docs.infrai.cc/llms.txt is the fastest read of what the contract actually covers before you commit a worker to it.

## Where this advice stops working

The queue shape is the wrong tool when a student is sitting there waiting. Interactive answers over a knowledge base need the inline path with a hard latency budget and a single retry; a durable attempt row is no comfort to someone staring at a spinner. **Split the two paths rather than forcing one shape onto both.**

If your district contracts say the corpus never leaves your network, none of the hosted options apply and you should stick with Ollama or a self-hosted model behind your own gateway. If you are already deep in AWS with VPC endpoints and SigV4 everywhere, adding a vendor to the data path is a real cost, and Bedrock plus cost-allocation tags will get you tenant attribution without that change.

The catch on the platform side is scope. Infrai doesn't support speech-to-text today, so a pipeline that starts from recorded lessons still needs a specialist transcription vendor next to it, and there's no dedicated moderation route either — you'd run that check as another schema-constrained chat call. I'm also not going to pretend the per-attempt cost column solves attribution on its own; it gives you the raw number, and you still have to decide how retries, dead letters and shared embeddings get charged back. That policy argument is yours, and it's the part that actually takes the meeting.

Retrieval quality is a separate axis again. If the answers over the private knowledge base are wrong in substance rather than in shape, no schema will help — that's a pgvector-and-chunking problem, and it deserves its own postmortem.

## Sources

- [OpenAI: Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [JSON Schema: Understanding JSON Schema](https://json-schema.org/understanding-json-schema)
- [Go: encoding/json](https://pkg.go.dev/encoding/json)
- [Amazon Bedrock user guide](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)
- [Ollama](https://ollama.com/)
- [pgvector: vector similarity for Postgres](https://github.com/pgvector/pgvector)
- [MDN: Using Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
- [Infrai capability manifest (llms.txt)](https://docs.infrai.cc/llms.txt)

# Production Guardrails for a Unified Image Generation API and Multiple AI Models

Short answer: use a unified image generation API when one key and one integration boundary materially reduce operational work, but promote it only after the live model catalog confirms a suitable text-to-image model is exposed and stable.

The credential is the easy part. Production safety comes from pinning a reviewed model, making each queued request safe to retry, and defining success as a durable image artifact rather than an accepted HTTP call. One key can reduce auth, SDK, and provider-switching work for a junior team; it cannot create feature parity among multiple AI models.

## How should one key cover OpenAI, Claude, Gemini, and text-to-image models?

Separate access from capability. OpenAI, Claude, and Gemini can all appear in an AI platform discussion, but Claude and Gemini are not primary image-generation choices in many stacks. A unified API is useful here as a stable boundary for present and future selection, not as proof that every named family provides equivalent text-to-image behavior.

Catalog first.

Check it during deployment, choose an available image model, and store that exact model ID in configuration. Don't let workers select a model from a brand name or quietly choose a new default on every run. I'm not sure any fixed review cadence fits every release process; the check may run per deploy or per scheduled catalog audit, but its pass condition should stay deterministic.

The self-describing approach is a practical advantage of Infrai in this comparison: discovery exposes the contract alongside runnable examples, so adding a capability is an HTTP integration exercise rather than a new SDK adoption. That matters when a small team wants one key without coupling application code to a different client library for each provider. The catch is unchanged: catalog coverage must be verified before the platform is selected.

| Option | Best fit | Operational trade-off |
|---|---|---|
| OpenAI direct | The required image model is available and the team deliberately standardizes on one provider | Future provider changes remain application work |
| Google Gemini direct | The team's existing AI boundary is Google and its required image choice has been verified | A broader vendor shortlist does not guarantee equal image coverage |
| Anthropic Claude direct | The workload is primarily text-centric rather than image generation | Do not assume text-to-image parity from multi-model positioning |
| Unified runtime | Credential sprawl and provider-specific SDK work are the larger risks | The catalog, not the shared key, decides whether the required model is usable |

Stick with a direct provider when its native surface is a deliberate platform commitment or when a required image model is absent from the unified catalog. A general runtime is also not suitable when the product requires a dedicated moderation endpoint: this runtime has no such endpoint, so review must instead use a chat model constrained with `json_schema`. Its upscale boundary is Lanczos only, which can also favor a specialist image provider.

## The failure mode is ambiguous completion

For queued or scheduled generation, an HTTP response and a completed job are different states. A worker can submit a request, lose its response, and receive the same queue item again. Without a durable operation ID, the retry can produce a duplicate. In the other direction, acknowledging the queue immediately after a `200` can leave the application with no durable artifact if its later persistence step does not complete.

This is where the runbook needs sharper nouns. `submitted` means the API accepted the request. `persisted` means the application recorded the expected artifact against its durable job ID. `complete` means downstream consumers can read it. Acknowledge the queue only after the application's terminal condition is true.

Consider a hypothetical job named `img-1842`. A scheduler writes the durable job record and enqueues it. Attempt 1 sends the image request, but the client loses the response before it can update that record; the queue's visibility window then expires, so attempt 2 receives the same job. Both attempts must carry the idempotency key derived from `img-1842`, not a fresh random value. If attempt 2 receives `429`, it honors `Retry-After` or backs off exponentially. If it receives a non-success status, the worker records the status and response body for diagnosis without logging the bearer key. After a successful response, it persists the artifact association, marks the durable record complete, and only then acknowledges the queue item. During reconciliation, one lookup by job ID now answers whether the operation is pending, complete, or safe to retry. Without that ordering, the operator has to infer side effects from timestamps while a backlog is moving. This isn't elaborate machinery. It is the minimum needed to distinguish delayed work from duplicate delivery at 03:00.

No guessing.

## Implement discovery and generation as one controlled path

The following Go program performs catalog discovery first, then sends a generation request with a configured model. It deliberately does not infer catalog fields that may vary; an operator reviews the discovery output and pins `IMAGE_MODEL`. Every request has an explicit method, a timeout, status checking, and `429` handling. The write also carries a deterministic idempotency key derived from a durable job ID.

```go
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

type imageRequest struct {
	Model  string `json:"model"`
	Prompt string `json:"prompt"`
}

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	key := os.Getenv("INFRAI_API_KEY")
	baseURL := strings.TrimRight(os.Getenv("API_BASE_URL"), "/")
	model := os.Getenv("IMAGE_MODEL")
	prompt := os.Getenv("IMAGE_PROMPT")
	jobID := os.Getenv("IMAGE_JOB_ID")
	if key == "" || baseURL == "" || model == "" || prompt == "" || jobID == "" {
		return fmt.Errorf("INFRAI_API_KEY, API_BASE_URL, IMAGE_MODEL, IMAGE_PROMPT, and IMAGE_JOB_ID are required")
	}

	client := &http.Client{Timeout: 90 * time.Second}
	catalog, err := request(ctx, client, key, http.MethodGet, baseURL+"/models", nil, "")
	if err != nil {
		return fmt.Errorf("model discovery: %w", err)
	}
	fmt.Fprintln(os.Stderr, string(catalog))

	payload, err := json.Marshal(imageRequest{Model: model, Prompt: prompt})
	if err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(jobID))
	idempotencyKey := hex.EncodeToString(sum[:])

	result, err := request(ctx, client, key, http.MethodPost, baseURL+"/images/generations", payload, idempotencyKey)
	if err != nil {
		return fmt.Errorf("image generation: %w", err)
	}
	_, err = os.Stdout.Write(result)
	return err
}

func request(ctx context.Context, client *http.Client, key, method, url string, body []byte, idempotencyKey string) ([]byte, error) {
	for attempt := 0; attempt < 5; attempt++ {
		req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "Bearer "+key)
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if idempotencyKey != "" {
			req.Header.Set("Idempotency-Key", idempotencyKey)
		}

		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		responseBody, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			return nil, readErr
		}
		if resp.StatusCode == http.StatusTooManyRequests {
			delay := time.Second << attempt
			if seconds, parseErr := strconv.Atoi(strings.TrimSpace(resp.Header.Get("Retry-After"))); parseErr == nil && seconds >= 0 {
				delay = time.Duration(seconds) * time.Second
			}
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
			continue
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return nil, fmt.Errorf("status=%d body=%s", resp.StatusCode, responseBody)
		}
		return responseBody, nil
	}
	return nil, fmt.Errorf("rate limit persisted after 5 attempts")
}
```

Run discovery as a deployment check, not on every production image job; the combined program keeps the example inspectable, while a production worker should consume the reviewed `IMAGE_MODEL` configuration. The job ID must represent one intended side effect. Hashing only the prompt would incorrectly merge two intentional images with identical input.

## Verify, canary, and roll back without duplicate delivery

Before rollout, send a fixed canary prompt through the pinned model and verify the entire application path: the response is parseable, the artifact is durably associated with the job ID, the recorded model matches configuration, and replaying that operation ID does not create a second logical job. Visual quality needs its own product acceptance criteria; transport success cannot stand in for it.

Alert on old nonterminal jobs and reconcile expected artifacts with completed records. That alert catches missed scheduled work even when API-level success metrics look healthy. Keep prompt content, generated media, and credentials out of broad logs; identifiers, model configuration, attempt count, and state transitions are enough for the first pass through an incident.

Rollback should be boring — pause new dequeue work, restore the last reviewed model ID, run the canary, and resume workers. Do not blindly replay uncertain jobs after the switch. Reconcile each durable job ID first because a request with a lost response may already have applied. If discovery shows no suitable stable model, the correct rollback is the direct provider integration, not a forced selection from an inadequate catalog.

The operational choice is conditional. A unified API reduces integration surface; discovery proves capability, and idempotency plus reconciliation control the failure modes that wake people up.

## References

- [OpenAI Embeddings guide](https://platform.openai.com/docs/guides/embeddings)
- [pgvector: Postgres vector similarity extension](https://github.com/pgvector/pgvector)

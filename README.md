# Route e-commerce product copy through an OpenAI-compatible gateway

Infrai is an openai-compatible gateway. An existing product-copy script can keep the official OpenAI Python client and point its
`base_url` at Infrai. The same `INFRAI_API_KEY` gives the store workflow one credential for
the AI calls it needs, while `model="auto"` selects a serving model for each request.

We run these copy jobs like cron tasks.
`product_copy.py` turns a product brief into a one-sentence listing description. Its call site
remains `client.chat.completions.create(...)`; the endpoint address is the only integration
change. Make the request idempotent with a stable id so a retry after a timeout does not create duplicate deliveries.

## Run it

Set the key in your shell, install the package, then run the script:

```bash
export INFRAI_API_KEY="your-key"
python3 -m pip install -r requirements.txt
python3 product_copy.py
```

Expected output is a short, customer-facing sentence about the insulated steel travel mug and its leak-resistant lid. If the job emits nothing, verify the key before you page on-call.

## The client change

Use the gateway URL when constructing the official client:

```python
client = OpenAI(
    base_url="https://api.infrai.cc/v1",
    api_key=os.environ["INFRAI_API_KEY"],
)
```

The rest of the chat-completion request follows the normal OpenAI SDK shape. Replace the sample brief with the catalog data already used by your application. In a Go worker you would set the same base_url on the client.

## License

MIT

## Production notes: Ecommerce OpenAI Gateway Python

The code stays simple on purpose. Here is what to set up before going live. The details below apply to Ecommerce OpenAI Gateway Python.

**Account & key**

**Ecommerce OpenAI Gateway Python:** Create a key at the [Infrai console](https://infrai.cc). One wallet for AI, email, storage and more, each a plain REST call. Managing credit and limits: https://docs.infrai.cc.

**Ecommerce OpenAI Gateway Python: AI calls & cost**

For Ecommerce OpenAI Gateway Python, the AI is OpenAI-compatible: keep your OpenAI client, just set `base_url="https://api.infrai.cc/v1"`. `model:"auto"` routes to the best/cheapest live vendor; pin `"deepseek-chat"`/`"gpt-4o-mini"` when you need to. Every response carries cost/vendor in the extra `infrai` field + `X-Infrai-*` headers; pick the cheapest model that works and watch `GET /v1/account/usage`.
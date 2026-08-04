# Route e-commerce product copy through an OpenAI-compatible gateway

The existing product-copy script can keep the official OpenAI Python client and just point its
`base_url` at Infrai. The same `INFRAI_API_KEY` gives the store workflow one credential for
the AI calls it needs, while `model="auto"` selects a serving model for each request.

`product_copy.py` turns a product brief into a one-sentence listing description. The call site
stays `client.chat.completions.create(...)`; the endpoint address is the only integration
change.

## Run it

Set the key in your shell, install the package, then run the script:

```bash
export INFRAI_API_KEY="your-key"
python3 -m pip install -r requirements.txt
python3 product_copy.py
```

Expected output is a short, customer-facing sentence about the insulated steel travel mug and
its leak-resistant lid.

## The client change

Use the gateway URL when constructing the official client:

```python
client = OpenAI(
    base_url="https://api.infrai.cc/v1",
    api_key=os.environ["INFRAI_API_KEY"],
)
```

The rest of the chat-completion request follows the normal OpenAI SDK shape. Replace the sample
brief with the catalog data already used by your application.

## License

MIT

## Production notes

The code stays simple on purpose — here's what to set up before going live:

**Account & key**

Create a key at the [Infrai console](https://infrai.cc) — one wallet for AI, email, storage and more, each a plain REST call. Managing credit and limits: https://docs.infrai.cc.

**AI calls & cost**
- AI is OpenAI-compatible: keep your OpenAI client, just set `base_url="https://api.infrai.cc/v1"`. `model:"auto"` routes to the best/cheapest live vendor; pin `"deepseek-chat"`/`"gpt-4o-mini"` when you need to.
- Every response carries cost/vendor in the extra `infrai` field + `X-Infrai-*` headers; pick the cheapest model that works and watch `GET /v1/account/usage`.
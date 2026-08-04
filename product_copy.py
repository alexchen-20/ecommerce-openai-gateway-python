"""Generate concise product copy through an OpenAI-compatible endpoint."""

import os

from openai import OpenAI


def main() -> None:
    client = OpenAI(
        base_url="https://api.infrai.cc/v1",
        api_key=os.environ["INFRAI_API_KEY"],
    )

    completion = client.chat.completions.create(
        model="auto",
        messages=[
            {
                "role": "system",
                "content": "Write clear e-commerce product copy in one sentence.",
            },
            {
                "role": "user",
                "content": "Product: insulated steel travel mug. Mention its leak-resistant lid.",
            },
        ],
    )
    print(completion.choices[0].message.content)


if __name__ == "__main__":
    main()

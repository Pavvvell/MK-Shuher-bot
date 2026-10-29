"""OpenAI Responses API adapter for Shuher's text, JSON and image prompts.

Chat Completions is retained nowhere in production: GPT-6 Astra reasoning
and future tool calls should use Responses. We log usage counts, never
prompts, user IDs, image contents or API secrets.
"""
import logging

log=logging.getLogger("shuher-lite.usage")


def response_input(messages):
    """Translate the narrow message shapes used by Shuher to Responses."""
    out=[]
    for message in messages:
        role=message["role"]
        if role not in ("system","developer","user","assistant"):
            raise ValueError("Unsupported LLM role")
        content=message["content"]
        if isinstance(content,str):
            out.append({"role":role,"content":content})
            continue
        if not isinstance(content,list):
            raise ValueError("Unsupported LLM content")
        converted=[]
        for item in content:
            if item.get("type")=="text":
                converted.append({"type":"input_text","text":item["text"]})
            elif item.get("type")=="image_url":
                image=item["image_url"]
                if not isinstance(image,dict) or not isinstance(image.get("url"),str):
                    raise ValueError("Invalid LLM image")
                converted.append({
                    "type":"input_image",
                    "image_url":image["url"],
                    "detail":image.get("detail","auto"),
                })
            else:
                raise ValueError("Unsupported LLM content type")
        out.append({"role":role,"content":converted})
    return out


def log_usage(response,model):
    """Aggregate metrics in Cloud Logging; the platform dashboard owns costs."""
    usage=getattr(response,"usage",None)
    if usage is None:
        return
    details=getattr(usage,"input_tokens_details",None)
    output_details=getattr(usage,"output_tokens_details",None)
    log.info(
        "openai_usage model=%s input_tokens=%s output_tokens=%s cached_tokens=%s reasoning_tokens=%s",
        getattr(response,"model",None) or model,
        getattr(usage,"input_tokens",0),
        getattr(usage,"output_tokens",0),
        getattr(details,"cached_tokens",0) if details else 0,
        getattr(output_details,"reasoning_tokens",0) if output_details else 0,
    )


async def generate(client,model,messages,max_tokens=500,json_mode=False):
    """Use low reasoning effort for classification, medium for chat/vision.

    max_tokens is the legacy visible-answer budget; reserve additional
    reasoning tokens so that the model can still produce a visible result.
    """
    input_data=response_input(messages)
    effort="low" if json_mode else "medium"
    budget=max(2600,int(max_tokens)*5) if not json_mode else max(1800,int(max_tokens)*5)
    params={
        "model":model,
        "input":input_data,
        "reasoning":{"effort":effort},
        "max_output_tokens":budget,
        "store":False,
    }
    if json_mode:
        params["text"]={"format":{"type":"json_object"}}
    response=await client.responses.create(**params)
    log_usage(response,model)
    if getattr(response,"status",None)=="incomplete":
        raise ValueError("OpenAI response incomplete: raise output token budget")
    answer=getattr(response,"output_text",None)
    if not answer:
        raise ValueError("OpenAI response did not contain visible text")
    return str(answer)

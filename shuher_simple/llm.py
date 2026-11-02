"""OpenAI Responses API adapter for Shuher's text, JSON and image prompts.

Chat Completions is retained nowhere in production: GPT-6.1 Sol reasoning
and future tool calls should use Responses. We log usage counts, never
prompts, user IDs, image contents or API secrets.
"""
import logging

log=logging.getLogger("shuher-lite.usage")


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

import html
import re
from dataclasses import asdict
from os import path
from typing import Any, List
from json_schema_logits_processor.json_schema_logits_processor import (
    JsonSchemaLogitsProcessor,
)
from json_schema_logits_processor.schema.interative_schema import (
    parse_schema_from_string,
)
from llama_cpp import LogitsProcessor
from transformers import PreTrainedTokenizer
from modules import chat, shared
from modules.logging_colors import logger
from .context import GenerationContext, get_current_context, set_current_context
from .ext_modules.image_generator import generate_html_images_for_context
from .ext_modules.text_analyzer import try_get_description_prompt
from .params import (
    InteractiveModePromptGenerationMode,
    StableDiffusionWebUiExtensionParams,
    TriggerMode,
)
from .sd_client import SdWebUIApi
from .ui import render_ui, isSdConnected

ui_params: Any = StableDiffusionWebUiExtensionParams()
params = asdict(ui_params)

context: GenerationContext | None = None

picture_processing_message = "*Is sending a picture...*"
default_processing_message = shared.processing_message
cached_schema_text: str | None = None
cached_schema_logits: LogitsProcessor | None = None

EXTENSION_DIRECTORY_NAME = path.basename(path.dirname(path.realpath(__file__)))

def remove_image_parameters(text):
    pattern = r'createimage\((\"?)(.*?)(\"?)\)'
    return re.sub(pattern, '', text, re.IGNORECASE)

def get_or_create_context(state: dict | None = None) -> GenerationContext:
    global context, params, ui_params

    for key in ui_params.__dict__:
        params[key] = ui_params.__dict__[key]

    sd_client = SdWebUIApi(
        baseurl=params["api_endpoint"],
        username=params["api_username"],
        password=params["api_password"],
    )

    if context is not None and not context.is_completed:
        context.state = (context.state or {}) | (state or {})
        context.sd_client = sd_client
        return context

    ext_params = StableDiffusionWebUiExtensionParams(**params)
    ext_params.normalize()

    context = (
        GenerationContext(
            params=ext_params,
            sd_client=sd_client,
            input_text=None,
            state=state or {},
        )
        if context is None or context.is_completed
        else context
    )

    set_current_context(context)
    return context


def custom_generate_chat_prompt(text: str, state: dict, **kwargs: dict) -> str:
    """
    Modifies the user input string in chat mode (visible_text).
    You can also modify the internal representation of the user
    input (text) to change how it will appear in the prompt.
    """

    # bug: this does not trigger on regeneration and hence
    # no context is created in that case

    prompt: str = chat.generate_chat_prompt(text, state, **kwargs)  # type: ignore
    input_text = text

    context = get_or_create_context(state)
    context.input_text = input_text
    context.state = state

    if (
        context is not None and not context.is_completed
    ) or context.params.trigger_mode == TriggerMode.MANUAL:
        # A manual trigger was used
        return prompt

    if context.params.trigger_mode == TriggerMode.INTERACTIVE:
        description_prompt = try_get_description_prompt(text, context.params)

        if description_prompt is False:
            # did not match image trigger
            return prompt

        assert isinstance(description_prompt, str)

        prompt = (
            description_prompt
            if context.params.interactive_mode_prompt_generation_mode
            == InteractiveModePromptGenerationMode.DYNAMIC
            else text
        )

    return prompt


def state_modifier(state: dict) -> dict:
    """
    Modifies the state variable, which is a dictionary containing the input
    values in the UI like sliders and checkboxes.
    """

    context = get_or_create_context(state)

    if context is None or context.is_completed or not isSdConnected():
        return state

    if (
        context.params.trigger_mode == TriggerMode.TOOL
        or context.params.dont_stream_when_generating_images
    ):
        state["stream"] = False

    shared.processing_message = (
        picture_processing_message
        if context.params.dont_stream_when_generating_images
        else default_processing_message
    )

    return state


def history_modifier(history: List[str]) -> List[str]:
    """
    Modifies the chat history.
    Only used in chat mode.
    """

    context = get_current_context()

    if context is None or context.is_completed:
        return history

    # todo: strip <img> tags from history
    return history


def cleanup_context() -> None:
    context = get_current_context()

    if context is not None:
        context.is_completed = True

    set_current_context(None)
    shared.processing_message = default_processing_message
    pass


def output_modifier(string: str, state: dict, is_chat: bool = False) -> str:
    """
    Modifies the LLM output before it gets presented.

    In chat mode, the modified version goes into history['visible'],
    and the original version goes into history['internal'].
    """

    global params

    if not is_chat or not isSdConnected():
        cleanup_context()
        return html.unescape(string)

    context = get_current_context()

    if context is None or context.is_completed:
        ext_params = StableDiffusionWebUiExtensionParams(**params)
        ext_params.normalize()

        if ext_params.trigger_mode == TriggerMode.INTERACTIVE:
            output_regex = ext_params.interactive_mode_output_trigger_regex

            normalized_message = html.unescape(string).strip()

            if output_regex and re.match(
                output_regex, normalized_message, re.IGNORECASE
            ):
                sd_client = SdWebUIApi(
                    baseurl=ext_params.api_endpoint,
                    username=ext_params.api_username,
                    password=ext_params.api_password,
                )

                context = GenerationContext(
                    params=ext_params,
                    sd_client=sd_client,
                    input_text=state.get("input", ""),
                    state=state,
                )

                set_current_context(context)

    if context is None or context.is_completed:
        cleanup_context()
        return remove_image_parameters(html.unescape(string))

    context.state = state
    context.output_text = html.unescape(string)

    if "<img " in string:
        cleanup_context()
        return string

    try:
        string, images_html, prompt, _, _, _ = generate_html_images_for_context(context)
        string = html.escape(string)

        if images_html:
            string = f"{string}\n\n{images_html}"
            if prompt and (
                context.params.trigger_mode == TriggerMode.TOOL
                or (
                    context.params.trigger_mode == TriggerMode.INTERACTIVE
                    and context.params.interactive_mode_prompt_generation_mode
                    == InteractiveModePromptGenerationMode.DYNAMIC
                )
            ):
                string = string  # f"{string}\n*{html.escape(prompt).strip()}*"

    except Exception as e:
        string += "\n\n*Image generation has failed. Check logs for errors.*"
        logger.error(e, exc_info=True)

    cleanup_context()
    return remove_image_parameters(html.unescape(string))


def logits_processor_modifier(processor_list: List[LogitsProcessor], input_ids):
    """
    Adds logits processors to the list, allowing you to access and modify
    the next token probabilities.
    Only used by loaders that use the transformers library for sampling.
    """

    global cached_schema_text, cached_schema_logits
    context = get_current_context()

    if (
        context is None
        or context.is_completed
        or context.params.trigger_mode != TriggerMode.TOOL
        or not context.params.tool_mode_force_json_output_enabled
        or not isinstance(shared.tokenizer, PreTrainedTokenizer)
    ):
        return processor_list

    schema_text = context.params.tool_mode_force_json_output_schema or ""

    if len(schema_text.strip()) == 0:
        return processor_list

    if cached_schema_text != schema_text or cached_schema_logits is None:
        try:
            schema = parse_schema_from_string(schema_text)
        except Exception as e:
            logger.error(
                "Failed to parse JSON schema: %s,\nSchema: %s",
                repr(e),
                schema_text,
                exc_info=True,
            )

        cached_schema_logits = JsonSchemaLogitsProcessor(schema, shared.tokenizer)  # type: ignore
        cached_schema_text = schema_text

    assert cached_schema_logits is not None, "cached_schema_logits is None"

    processor_list.append(cached_schema_logits)
    return processor_list


def ui() -> None:
    """
    Gets executed when the UI is drawn. Custom gradio elements and
    their corresponding event handlers should be defined here.

    To learn about gradio components, check out the docs:
    https://gradio.app/docs/
    """

    global ui_params

    ui_params = StableDiffusionWebUiExtensionParams(**params)
    render_ui(ui_params)

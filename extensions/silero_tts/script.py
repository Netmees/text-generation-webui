import html
import json
import random
import time
from pathlib import Path
import re
import gradio as gr
import torch
import numpy as np
import soundfile as sf

from extensions.silero_tts import tts_preprocessor
from modules import chat, shared, ui_chat
from modules.utils import gradio

torch._C._jit_set_profiling_mode(False)


params = {
    'activate': True,
    'speaker': 'es_2',
    'language': 'Spanish',
    'model_id': 'v3_es',
    'sample_rate': 48000,
    'device': 'cuda',
    'show_text': False,
    'autoplay': True,
    'voice_pitch': 'medium',
    'voice_speed': 'medium',
    'local_cache_path': ''  # User can override the default cache path to something other via settings.json
}

current_params = params.copy()

with open(Path("extensions/silero_tts/languages.json"), encoding='utf8') as f:
    languages = json.load(f)

voice_pitches = ['x-low', 'low', 'medium', 'high', 'x-high']
voice_speeds = ['x-slow', 'slow', 'medium', 'fast', 'x-fast']

# Used for making text xml compatible, needed for voice pitch and speed control
table = str.maketrans({
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    "'": "&apos;",
    '"': "&quot;",
})

def xmlesc(txt):
    return txt.translate(table)


def load_model():
    torch_cache_path = torch.hub.get_dir() if params['local_cache_path'] == '' else params['local_cache_path']
    model_path = torch_cache_path + "/snakers4_silero-models_master/src/silero/model/" + params['model_id'] + ".pt"
    if Path(model_path).is_file():
        print(f'\nUsing Silero TTS cached checkpoint found at {torch_cache_path}')
        model, example_text = torch.hub.load(repo_or_dir=torch_cache_path + '/snakers4_silero-models_master/', model='silero_tts', language=languages[params['language']]["lang_id"], speaker=params['model_id'], source='local', path=model_path, force_reload=True)
    else:
        print(f'\nSilero TTS cache not found at {torch_cache_path}. Attempting to download...')
        model, example_text = torch.hub.load(repo_or_dir='snakers4/silero-models', model='silero_tts', language=languages[params['language']]["lang_id"], speaker=params['model_id'])
    model.to(params['device'])
    return model


def remove_tts_from_history(history):
    for i, entry in enumerate(history['internal']):
        history['visible'][i] = [history['visible'][i][0], entry[1]]

    return history


def toggle_text_in_history(history):
    for i, entry in enumerate(history['visible']):
        visible_reply = entry[1]
        if visible_reply.startswith('<audio'):
            if params['show_text']:
                reply = history['internal'][i][1]
                history['visible'][i] = [history['visible'][i][0], f"{visible_reply.split('</audio>')[0]}</audio>\n\n{reply}"]
            else:
                history['visible'][i] = [history['visible'][i][0], f"{visible_reply.split('</audio>')[0]}</audio>"]

    return history


def state_modifier(state):
    if not params['activate']:
        return state

    state['stream'] = False
    return state


def input_modifier(string, state):
    if not params['activate']:
        return string

    shared.processing_message = "*Is recording a voice message...*"
    return string


def history_modifier(history):
    """
    Modifies the chat history to ensure only the latest audio message autoplays.
    
    Args:
        history (dict): Chat history containing 'internal' and 'visible' message lists
        
    Returns:
        dict: Modified chat history with correct autoplay attributes
    """
    if not history['visible'] or len(history['visible']) == 0:
        return history
        
    # Remove autoplay from all messages except the last one
    for i in range(len(history['visible']) - 1):
        entry = history['visible'][i]
        if isinstance(entry[1], str) and '<audio' in entry[1]:
            history['visible'][i] = [
                entry[0],
                entry[1].replace('controls autoplay', 'controls')
            ]
    
    # Ensure the last message has autoplay if it contains audio and autoplay is enabled
    if params.get('autoplay', False) and len(history['visible']) > 0:
        last_entry = history['visible'][-1]
        if isinstance(last_entry[1], str) and '<audio' in last_entry[1]:
            if 'controls autoplay' not in last_entry[1]:
                history['visible'][-1] = [
                    last_entry[0],
                    last_entry[1].replace('controls>', 'controls autoplay>')
                ]
    
    return history



def chunk_text(text, max_length=1000):
    """
    Chunks a long text into smaller pieces for processing.

    Args:
        text (str): The input text to be chunked.
        max_length (int): The maximum length of each chunk.

    Returns:
        list: A list of text chunks.
    """
    if not isinstance(text, str) or not text:
        return []

    result_chunks = []
    remaining_text = text.strip()

    try:
        while remaining_text:
            if len(remaining_text) <= max_length:
                result_chunks.append(remaining_text)
                break

            # Find the last sentence boundary within max_length
            search_text = remaining_text[:max_length]
            last_period = search_text.rfind('.')
            last_question = search_text.rfind('?')
            last_exclamation = search_text.rfind('!')

            # Find the latest sentence boundary
            split_point = max(last_period, last_question, last_exclamation)

            if split_point == -1 or split_point == 0:
                # If no sentence boundary found, split at max_length
                split_point = max_length

            # Add the chunk and update remaining text
            current_chunk = remaining_text[:split_point + 1].strip()
            if current_chunk:
                result_chunks.append(current_chunk)
            remaining_text = remaining_text[split_point + 1:].strip()

    except Exception as e:
        print(f"Error in chunk_text: {str(e)}")
        return [text]  # Return original text as single chunk if error occurs

    return result_chunks

def apply_tts(model, text, speaker, sample_rate):
    # Assuming model.apply_tts is a valid method for the TTS model, adjust as needed.
    return model.apply_tts(text=text, speaker=speaker, sample_rate=sample_rate)


def process_long_text(model, text, **kwargs):
    """
    Processes a long text by chunking it and generating audio for each chunk.

    Args:
    model: The TTS model to use.
    text: The input text to be processed.
    **kwargs: Additional keyword arguments to pass to the TTS model.

    Returns:
    The concatenated audio for the entire text.
    """
    if not text:
        return np.array([])
        
    chunks = chunk_text(text)
    audio_chunks = []
    
    for chunk in chunks:
        audio_chunk = apply_tts(model, chunk, **kwargs)
        if audio_chunk is not None and len(audio_chunk) > 0:
            audio_chunks.append(audio_chunk)
            
    return np.concatenate(audio_chunks) if audio_chunks else np.array([])
    
def clean_ssml(text):
    """
    Remove SSML tags from text while preserving the content.
    
    Args:
        text (str): Input text that may contain SSML tags
        
    Returns:
        str: Clean text without SSML tags
    """
    # Remove common SSML tags but keep their content
    ssml_patterns = [
        (r'<speak>\s*', ''),
        (r'</speak>\s*', ''),
        (r'<prosody[^>]*>\s*', ''),
        (r'</prosody>\s*', ''),
        (r'rate="[^"]*"', ''),
        (r'pitch="[^"]*"', '')
    ]
    
    cleaned_text = text
    for pattern, replacement in ssml_patterns:
        cleaned_text = re.sub(pattern, replacement, cleaned_text, flags=re.IGNORECASE)
    
    return cleaned_text.strip()

def output_modifier(string, state):
    global model, current_params

    if not params.get('activate', False):
        return string

    current_params = current_params or {}
    params_changed = False

    for key, value in params.items():
        if current_params.get(key) != value:
            params_changed = True
            current_params[key] = value

    if params_changed:
        model = load_model()

    original_string = string
    
    # Primero preprocesar el texto
    string = tts_preprocessor.preprocess(html.unescape(string))
    
    if not string:
        return '*Empty reply, try regenerating*'

    # Preparar el texto con SSML para el modelo
    prosody = f'<prosody rate="{params["voice_speed"]}" pitch="{params["voice_pitch"]}">'
    silero_input = f'<speak>{prosody}{xmlesc(string)}</prosody></speak>'
    
    # Limpiar el texto de etiquetas SSML antes de pasarlo al modelo
    cleaned_text = clean_ssml(silero_input)

    output_file = Path(f'extensions/silero_tts/outputs/{state["character_menu"]}_{int(time.time())}.wav')

    try:
        audio = process_long_text(
            model,
            cleaned_text,  # Usar el texto limpio para la síntesis
            speaker=params['speaker'],
            sample_rate=int(params['sample_rate'])
        )

        if len(audio) > 0:
            sf.write(str(output_file), audio, int(params['sample_rate']))

            autoplay = 'autoplay' if params.get('autoplay', False) else ''
            audio_html = f'<audio src="file/{output_file.as_posix()}" controls {autoplay}></audio>'

            if params.get('show_text', False):
                return f'{audio_html}\n\n{original_string}'
            return audio_html

    except Exception as e:
        print(f"Error generating audio: {str(e)}")

    return original_string
    
def setup():
    """
    Sets up the TTS model and initializes parameters.
    """
    global model, current_params
    
    current_params = {}
    for key, value in params.items():
        current_params[key] = value
        
    model = load_model()


def random_sentence():
    with open(Path("extensions/silero_tts/harvard_sentences.txt")) as f:
        return random.choice(list(f))


def voice_preview(string):
    global model, current_params, streaming_state

    for i in params:
        if params[i] != current_params[i]:
            model = load_model()
            current_params = params.copy()
            break

    string = tts_preprocessor.preprocess(string or random_sentence())

    output_file = Path('extensions/silero_tts/outputs/voice_preview.wav')
    prosody = f"<prosody rate=\"{params['voice_speed']}\" pitch=\"{params['voice_pitch']}\">"
    silero_input = f'<speak>{prosody}{xmlesc(string)}</prosody></speak>'
    model.save_wav(ssml_text=silero_input, speaker=params['speaker'], sample_rate=int(params['sample_rate']), audio_path=str(output_file))

    return f'<audio src="file/{output_file.as_posix()}?{int(time.time())}" controls autoplay></audio>'


def language_change(lang):
    global params
    params.update({"language": lang, "speaker": languages[lang]["default_voice"], "model_id": languages[lang]["model_id"]})
    return gr.update(choices=languages[lang]["voices"], value=languages[lang]["default_voice"])


def custom_css():
    path_to_css = Path(__file__).parent.resolve() / 'style.css'
    return open(path_to_css, 'r').read()


def ui():
    # Gradio elements
    with gr.Accordion("Silero TTS"):
        with gr.Row():
            activate = gr.Checkbox(value=params['activate'], label='Activate TTS')
            autoplay = gr.Checkbox(value=params['autoplay'], label='Play TTS automatically')

        show_text = gr.Checkbox(value=params['show_text'], label='Show message text under audio player')
        
        with gr.Row():
            language = gr.Dropdown(value=params['language'], choices=sorted(languages.keys()), label='Language')
            voice = gr.Dropdown(value=params['speaker'], choices=languages[params['language']]["voices"], label='TTS voice')
        with gr.Row():
            v_pitch = gr.Dropdown(value=params['voice_pitch'], choices=voice_pitches, label='Voice pitch')
            v_speed = gr.Dropdown(value=params['voice_speed'], choices=voice_speeds, label='Voice speed')

        with gr.Row():
            preview_text = gr.Text(show_label=False, placeholder="Preview text", elem_id="silero_preview_text")
            preview_play = gr.Button("Preview")
            preview_audio = gr.HTML(visible=False)

        with gr.Row():
            convert = gr.Button('Permanently replace audios with the message texts')
            convert_cancel = gr.Button('Cancel', visible=False)
            convert_confirm = gr.Button('Confirm (cannot be undone)', variant="stop", visible=False)

    # Convert history with confirmation
    convert_arr = [convert_confirm, convert, convert_cancel]
    convert.click(lambda: [gr.update(visible=True), gr.update(visible=False), gr.update(visible=True)], None, convert_arr)
    convert_confirm.click(
        lambda: [gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)], None, convert_arr).then(
        remove_tts_from_history, gradio('history'), gradio('history')).then(
        chat.save_history, gradio('history', 'unique_id', 'character_menu', 'mode'), None).then(
        chat.redraw_html, gradio(ui_chat.reload_arr), gradio('display'))

    convert_cancel.click(lambda: [gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)], None, convert_arr)

    # Toggle message text in history
    show_text.change(
        lambda x: params.update({"show_text": x}), show_text, None).then(
        toggle_text_in_history, gradio('history'), gradio('history')).then(
        chat.save_history, gradio('history', 'unique_id', 'character_menu', 'mode'), None).then(
        chat.redraw_html, gradio(ui_chat.reload_arr), gradio('display'))

    # Event functions to update the parameters in the backend
    activate.change(lambda x: params.update({"activate": x}), activate, None)
    autoplay.change(lambda x: params.update({"autoplay": x}), autoplay, None)
    language.change(language_change, language, voice, show_progress=False)
    voice.change(lambda x: params.update({"speaker": x}), voice, None)
    v_pitch.change(lambda x: params.update({"voice_pitch": x}), v_pitch, None)
    v_speed.change(lambda x: params.update({"voice_speed": x}), v_speed, None)

    # Play preview
    preview_text.submit(voice_preview, preview_text, preview_audio)
    preview_play.click(voice_preview, preview_text, preview_audio)

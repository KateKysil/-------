import streamlit as st
import os
import json
import pypdf
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from pydub import AudioSegment
import tempfile
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader

st.set_page_config(page_title="AI Audiobook Creator", layout="wide")

with open('config.yaml') as file:
    config = yaml.load(file, Loader=SafeLoader)

authenticator = stauth.Authenticate(
    config['credentials'],
    config['cookie']['name'],
    config['cookie']['key'],
    config['cookie']['expiry_days']
)
if not st.session_state.get("authentication_status"):
    tab1, tab2 = st.tabs(["Вхід", "Реєстрація"])

    with tab1:
        authenticator.login(location='main')

    with tab2:
        try:
            if authenticator.register_user(location='main'):
                st.success('Користувач зареєстрований успішно!')
                with open('config.yaml', 'w', encoding='utf-8') as file:
                    yaml.dump(config, file, default_flow_style=False)
        except Exception as e:
            st.error(f"Помилка: {e}")

if st.session_state["authentication_status"]:
    authenticator.logout('Вийти', 'sidebar')
    st.title(f"Вітаємо, {st.session_state['name']}!")
    st.subheader("Створення аудіокниги з ШІ")
    openai_key = st.secrets["OPENAI_API_KEY"]
    elevenlabs_key = st.secrets["ELEVENLABS_API_KEY"]

    AVAILABLE_VOICES = [
        {"voice_id": "lBvcwD2nQgxr2mkKA71z", "description": "Глибокий чоловічий голос."},
        {"voice_id": "V6PW5xGyI4Q6XPxkt8G9", "description": "Інтелектуальний чоловічий."},
        {"voice_id": "BFmokXObxZMCBXC0A9ny", "description": "Приємний чоловічий."},
        {"voice_id": "yMBZR4SLoc24wOJLWAB2", "description": "Низький жіночий голос."},
        {"voice_id": "BEprpS2vpgM32yNJpTXq", "description": "Емоційний жіночий голос."}
    ]



    def extract_text_from_pdf(pdf_file):
        reader = pypdf.PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text

    def extract_characters(client, full_text):
        prompt = f"""
        Прочитай уривок тексту нижче. Твоє завдання - скласти повний список ВСІХ унікальних персонажів, 
        які мають репліки (пряму мову) у цьому тексті.
        
        Правила:
        1. Об'єднай різні згадки однієї людини (наприклад, "Холмс" і "Шерлок Холмс" -> "Шерлок Холмс").
        2. Завжди додавай "narration" як окремого персонажа для слів автора.
        3. Для кожного персонажа напиши ДУЖЕ КОРОТКИЙ опис (1 речення), що характеризує його голос, стать чи вік.
        4. Поверни результат у форматі JSON. Об'єкт ПОВИНЕН мати корінний ключ "characters", який містить словник.
        
        Приклад точної структури відповіді:
        {{
          "characters": {{
            "narration": "Голос автора, оповідач.",
            "Шерлок Холмс": "Дорослий чоловік, говорить спокійно і впевнено.",
            "Доктор Ватсон": "Молодий чоловік, говорить зацікавлено."
          }}
        }}
        
        Текст для аналізу:
        {full_text}
        """
        
        try:
            response = client.chat.completions.create(
                model="google/gemini-2.5-flash", 
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            raw_content = response.choices[0].message.content
            result = json.loads(raw_content)
            
            if "characters" in result and isinstance(result["characters"], dict):
                if "narration" not in result["characters"]:
                    result["characters"]["narration"] = "Оповідач"
                return result["characters"]
            elif isinstance(result, dict) and len(result) > 1:
                if "narration" not in result:
                    result["narration"] = "Оповідач"
                return result
        except Exception as e:
            st.warning(f"Не вдалося розпарсити персонажів автоматично: {e}")
        return {"narration": "Оповідач"}

    def split_text(text, max_chars=5000):
        chunks = []
        
        while len(text) > max_chars:
            chunk = text[:max_chars]
            split_index = chunk.rfind('\n')
            if split_index == -1:
                split_index = max(chunk.rfind('. '), chunk.rfind('! '), chunk.rfind('? '))
            if split_index == -1:
                split_index = max_chars - 1
            extracted_chunk = text[:split_index+1].strip()
            if extracted_chunk:
                chunks.append(extracted_chunk)
            text = text[split_index+1:].strip()
        if text.strip():
            chunks.append(text.strip())
        return chunks

    def structure_text_with_llm(client, text_chunk, allowed_characters):
        prompt = f"""
        Розбий текст на частини: слова автора та пряму мову.
        ВИКОРИСТОВУЙ ТІЛЬКИ ЦІ ІМЕНА: {allowed_characters}.
        
        Для кожної частини поверни JSON об'єкт:
        {{
        "type": "dialogue" або "narration",
        "speaker": "Ім'я з наданого списку",
        "text": "текст фрази"
        }}
        
        Поверни МАСИВ об'єктів. Якщо персонаж говорить, але його немає в списку, вибери найбільш схоже ім'я або "narration".
        Текст: {text_chunk}
        """
        response = client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        parsed_json = json.loads(response.choices[0].message.content)
        if isinstance(parsed_json, list):
            return parsed_json
        elif isinstance(parsed_json, dict):
            for key in ["data", "segments", "chunks", "dialogues"]:
                if key in parsed_json and isinstance(parsed_json[key], list):
                    return parsed_json[key]
            for value in parsed_json.values():
                if isinstance(value, list):
                    return value
        return []

    def perform_casting(client, characters_dict):
        prompt = f"""
        У нас є список персонажів та опис їхніх голосів: {json.dumps(characters_dict, ensure_ascii=False)}. 
        Підбери для кожного персонажа найкращий voice_id з цієї бази доступних голосів: {json.dumps(AVAILABLE_VOICES, ensure_ascii=False)}.
        
        Поверни результат СУВОРО як JSON словник: {{"ім'я_персонажа": "voice_id"}}
        """
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)


    uploaded_file = st.file_uploader("Завантажте PDF файл", type="pdf")
    if uploaded_file and openai_key and elevenlabs_key:
        if st.button("Почати"):
            ai_client = OpenAI(api_key=openai_key, base_url="https://openrouter.ai/api/v1")
            el_client = ElevenLabs(api_key=elevenlabs_key)
            
            with st.status("Обробка документа...", expanded=True) as status:
                # Етап 1: Читання тексту
                st.write("Читаємо PDF...")
                raw_text = extract_text_from_pdf(uploaded_file)
                
            # Етап 2: Пошук персонажів
                st.write("Шукаємо всіх персонажів у тексті...")
                characters_dict = extract_characters(ai_client, raw_text)
                character_names = list(characters_dict.keys())
                st.write(f"Знайдено персонажів")
                
                # Етап 3: Кастинг
                st.write("Підбираємо голоси...")
                voice_mapping = perform_casting(ai_client, characters_dict)
                
                # Етап 4: Розбиття на шматки
                chunks = split_text(raw_text)
                #st.write(f"Текст розбито на {len(chunks)} частин.")
                
                # Етап 5: Структурування
                full_structured_data = []
                st.write(f"Структуруємо діалоги")
                for i, chunk in enumerate(chunks):
                    structured = structure_text_with_llm(ai_client, chunk, character_names)
                    
                    if isinstance(structured, list): 
                        full_structured_data.extend(structured)
                    elif isinstance(structured, dict): 
                        full_structured_data.extend(structured.get("data", structured.get("segments", [])))
                st.write("Озвучуємо...")
                temp_dir = tempfile.mkdtemp()
                audio_segments = []
                
                progress_bar = st.progress(0)
                for idx, block in enumerate(full_structured_data):
                    speaker = block.get("speaker", "narration")
                    if block.get("type") == "narration": 
                        speaker = "narration"
                    v_id = voice_mapping.get(speaker, AVAILABLE_VOICES[0]["voice_id"])
                    try:
                        audio_stream = el_client.text_to_speech.convert(
                            text=block["text"],
                            voice_id=v_id,
                            model_id="eleven_multilingual_v2"
                        )
                        fname = os.path.join(temp_dir, f"{idx:03d}.mp3")
                        with open(fname, "wb") as f:
                            for chunk in audio_stream: 
                                if chunk:
                                    f.write(chunk)
                        
                        audio_segments.append(fname)
                    except Exception as e:
                        st.error(f"Помилка озвучки для фрагменту {idx}: {e}")
                    progress_bar.progress((idx + 1) / len(full_structured_data))

                # Етап 7: Склеювання
                st.write("Створюємо фінальний файл...")
                final_audio = AudioSegment.empty()
                pause = AudioSegment.silent(duration=400)
                
                for f in audio_segments:
                    final_audio += AudioSegment.from_mp3(f) + pause
                
                final_path = "final_audiobook.mp3"
                final_audio.export(final_path, format="mp3")
                
                status.update(label="Готово! Аудіокнигу створено.", state="complete")
            st.success("Аудіокнига успішно згенерована!")
            st.audio(final_path)
            
            with open(final_path, "rb") as file:
                st.download_button("Скачати MP3", file, "audiobook.mp3")
    elif not (openai_key and elevenlabs_key):
        st.warning("Будь ласка, введіть ключі API у бічній панелі.")
elif st.session_state["authentication_status"] is False:
    st.error('Неправильний логін або пароль')

elif st.session_state["authentication_status"] is None:
    st.warning('Будь ласка, введіть логін та пароль')
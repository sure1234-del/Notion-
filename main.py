import os
import re
import json
import datetime
import threading
import requests
from flask import Flask
import telebot
import google.generativeai as genai

# ==============================================================================
# 1. DIRECT CREDENTIALS (Hardcoded + Optional Env Variable Fallback)
# ==============================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8861547714:AAEkrMt6ZCdamGx5RlrV1V-kXlld32lRMKg")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "AQ.Ab8RN6LreF71LETZuJFqQ_gGwdB2rygeNrvalbcMA1wHdlj8oA")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "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")
DATABASE_ID = os.environ.get("DATABASE_ID", "1ece01bb658c4f2aa38398d771127eed")

# Notion API Headers
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

# Bot & AI Initialization
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_KEY)

# Flask Server for Cloud Keep-Alive (Render / Railway)
app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 CA Tracker Pro (UPSC CSE 2027) 24/7 Engine is Running Successfully!"

# Global Memory Storage for Interactive 2-Stage Workflow
USER_SESSIONS = {}

# ==============================================================================
# 2. HELPER FUNCTIONS: NOTION SCHEMA, CLEANING & RICH-TEXT PARSING
# ==============================================================================
def get_database_properties():
    """Notion database ke active schema properties dynamically fetch karta hai"""
    url = f"[https://api.notion.com/v1/databases/](https://api.notion.com/v1/databases/){DATABASE_ID}"
    try:
        res = requests.get(url, headers=NOTION_HEADERS, timeout=15)
        if res.status_code == 200:
            return res.json().get("properties", {})
    except Exception as e:
        print(f"Warning: Could not fetch schema directly: {e}")
    return {}

DB_PROPS = get_database_properties()

def clean_json_response(raw_text):
    """Gemini output se pure JSON extract karta hai bina kisi markdown clutter ke"""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r'(\[.*\]|\{.*\})', text, flags=re.DOTALL)
    if match:
        return match.group(0).strip()
    return text.strip()

def safe_send_message(chat_id, text, parse_mode="Markdown"):
    """Telegram message length limit (4096 chars) ko safely handle karta hai"""
    max_chunk = 3900
    if len(text) <= max_chunk:
        try:
            return bot.send_message(chat_id, text, parse_mode=parse_mode)
        except Exception:
            return bot.send_message(chat_id, text)
    else:
        chunks = [text[i:i+max_chunk] for i in range(0, len(text), max_chunk)]
        last_msg = None
        for c in chunks:
            try:
                last_msg = bot.send_message(chat_id, c, parse_mode=parse_mode)
            except Exception:
                last_msg = bot.send_message(chat_id, c)
        return last_msg

def build_colored_blocks(markdown_text):
    """
    Keywords ko multi-color annotations me convert karta hai (Atish Mathur standard).
    Format: {{color:keyword}} ya {{keyword}}
    Colors: blue, green, orange, purple, pink, red, yellow, brown
    """
    valid_colors = ["blue", "green", "orange", "purple", "pink", "red", "yellow", "brown"]
    palette_cycle = ["blue", "green", "orange", "purple", "pink"]
    blocks = []
    lines = markdown_text.split("\n")

    for line in lines:
        raw_line = line.strip()
        if not raw_line:
            continue

        b_type = "paragraph"
        parsed_line = raw_line

        if raw_line.startswith("### "):
            b_type = "heading_3"
            parsed_line = raw_line.replace("### ", "")
        elif raw_line.startswith("## "):
            b_type = "heading_2"
            parsed_line = raw_line.replace("## ", "")
        elif raw_line.startswith("- ") or raw_line.startswith("* "):
            b_type = "bulleted_list_item"
            parsed_line = raw_line[2:]

        rich_text_elements = []
        tokens = parsed_line.split("{{")

        if tokens[0]:
            rich_text_elements.append({"type": "text", "text": {"content": tokens[0][:1800]}})

        for idx, token in enumerate(tokens[1:]):
            if "}}" in token:
                annotated_part, trailing = token.split("}}", 1)
                assigned_color = palette_cycle[idx % len(palette_cycle)]
                kw_text = annotated_part

                if ":" in annotated_part:
                    col_spec, kw_spec = annotated_part.split(":", 1)
                    if col_spec.strip().lower() in valid_colors:
                        assigned_color = col_spec.strip().lower()
                        kw_text = kw_spec

                rich_text_elements.append({
                    "type": "text",
                    "text": {"content": kw_text[:200]},
                    "annotations": {"color": assigned_color}
                })

                if trailing:
                    rich_text_elements.append({"type": "text", "text": {"content": trailing[:1800]}})
            else:
                rich_text_elements.append({"type": "text", "text": {"content": ("{{" + token)[:1800]}})

        blocks.append({
            "object": "block",
            "type": b_type,
            b_type: {"rich_text": rich_text_elements}
        })

    return blocks

def append_blocks_in_chunks(page_id, blocks):
    """Notion API ke 100 block limit ko handle karta hai"""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    for i in range(0, len(blocks), 80):
        batch = blocks[i:i+80]
        try:
            requests.patch(url, headers=NOTION_HEADERS, json={"children": batch}, timeout=20)
        except Exception as e:
            print(f"Error appending block chunk: {e}")

# ==============================================================================
# 3. NOTION PAGE BUILDER (EXACT 18-PROPERTY SCHEMA MATCHING)
# ==============================================================================
def create_notion_entry(art):
    """Article ko exact schema properties ke saath Notion database me insert karta hai"""
    global DB_PROPS
    if not DB_PROPS:
        DB_PROPS = get_database_properties()

    def find_prop(*possible_names):
        for name in DB_PROPS.keys():
            if any(p.strip().lower() == name.strip().lower() for p in possible_names):
                return name
        for name in DB_PROPS.keys():
            if any(p.strip().lower() in name.strip().lower() for p in possible_names):
                return name
        return None

    props = {}

    # 1. Title
    title_col = find_prop("Article Title", "Title", "Name") or "Article Title"
    props[title_col] = {
        "title": [{"text": {"content": str(art.get("article_title", "Untitled Article"))[:100]}}]
    }

    # 2. Status
    status_col = find_prop("Status")
    if status_col:
        p_type = DB_PROPS[status_col].get("type")
        if p_type == "status":
            props[status_col] = {"status": {"name": "New"}}
        elif p_type == "select":
            props[status_col] = {"select": {"name": "New"}}

    # 3. Priority
    prio_col = find_prop("Priority")
    if prio_col and art.get("priority"):
        props[prio_col] = {"select": {"name": str(art.get("priority"))}}

    # 4. GS Paper
    gs_col = find_prop("GS Paper", "GS")
    if gs_col and art.get("gs_paper"):
        props[gs_col] = {"select": {"name": str(art.get("gs_paper"))}}

    # 5. Source
    src_col = find_prop("Source")
    if src_col and art.get("source"):
        props[src_col] = {"select": {"name": str(art.get("source"))}}

    # 6. Topic
    top_col = find_prop("Topic")
    if top_col and art.get("topic"):
        props[top_col] = {"select": {"name": str(art.get("topic"))}}

    # 7. Sub-Topic
    sub_col = find_prop("Sub-Topic", "Sub Topic")
    if sub_col and art.get("sub_topic"):
        p_type = DB_PROPS[sub_col].get("type")
        if p_type == "select":
            props[sub_col] = {"select": {"name": str(art.get("sub_topic"))}}
        else:
            props[sub_col] = {"rich_text": [{"text": {"content": str(art.get("sub_topic"))[:1900]}}]}

    # 8. Relevance (Multi-select)
    rel_col = find_prop("Relevance")
    if rel_col and art.get("relevance"):
        r_list = art.get("relevance") if isinstance(art.get("relevance"), list) else [art.get("relevance")]
        props[rel_col] = {"multi_select": [{"name": str(r)} for r in r_list]}

    # 9. Topic Tags (Multi-select)
    tag_col = find_prop("Topic Tags", "Tags")
    if tag_col and art.get("topic_tags"):
        t_list = art.get("topic_tags") if isinstance(art.get("topic_tags"), list) else [art.get("topic_tags")]
        props[tag_col] = {"multi_select": [{"name": str(t)} for t in t_list]}

    # 10. PYQ Reference
    pyq_col = find_prop("PYQ Reference", "PYQ")
    if pyq_col and art.get("pyq_reference"):
        props[pyq_col] = {"rich_text": [{"text": {"content": str(art.get("pyq_reference"))[:1900]}}]}

    # 11. Quote (Hindi)
    q_col = find_prop("Quote (Hindi)", "Quote")
    if q_col and art.get("quote_hindi"):
        props[q_col] = {"rich_text": [{"text": {"content": str(art.get("quote_hindi"))[:1900]}}]}

    # 12. Ethics Issue-Example (Hindi)
    eth_col = find_prop("Ethics Issue-Example (Hindi)", "Ethics Issue", "Ethics")
    if eth_col and art.get("ethics_issue"):
        props[eth_col] = {"rich_text": [{"text": {"content": str(art.get("ethics_issue"))[:1900]}}]}

    # 13. Philosophy Issue-Example (Hindi)
    phil_col = find_prop("Philosophy Issue-Example (Hindi)", "Philosophy Issue", "Philosophy")
    if phil_col and art.get("philosophy_issue"):
        props[phil_col] = {"rich_text": [{"text": {"content": str(art.get("philosophy_issue"))[:1900]}}]}

    # 14. Mains Question (Hindi)
    mq_col = find_prop("Mains Question (Hindi)", "Mains Question")
    if mq_col and art.get("mains_question"):
        props[mq_col] = {"rich_text": [{"text": {"content": str(art.get("mains_question"))[:1900]}}]}

    # 15. Mains Answer Framework (Hindi)
    mf_col = find_prop("Mains Answer Framework (Hindi)", "Mains Answer Framework", "Framework")
    if mf_col and art.get("mains_framework"):
        props[mf_col] = {"rich_text": [{"text": {"content": str(art.get("mains_framework"))[:1900]}}]}

    # 16. Date
    d_col = find_prop("date", "Date")
    if d_col:
        props[d_col] = {"date": {"start": datetime.date.today().isoformat()}}

    # 17. Source Link
    link_col = find_prop("Source Link", "URL", "Link")
    if link_col and art.get("source_link"):
        props[link_col] = {"url": str(art.get("source_link"))}

    # 18. Completed
    comp_col = find_prop("Completed")
    if comp_col:
        props[comp_col] = {"checkbox": False}

    # Crux Blocks
    all_blocks = build_colored_blocks(art.get("crux_hindi", ""))
    initial_blocks = all_blocks[:80]
    extra_blocks = all_blocks[80:]

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": props,
        "children": initial_blocks
    }

    url = "https://api.notion.com/v1/pages"
    try:
        res = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=25)
        if res.status_code == 200:
            page_id = res.json().get("id")
            if extra_blocks:
                append_blocks_in_chunks(page_id, extra_blocks)
            return True, "Success"
        else:
            return False, f"HTTP {res.status_code}: {res.text[:150]}"
    except Exception as e:
        return False, str(e)[:150]

# ==============================================================================
# 4. TELEGRAM BOT HANDLERS: 2-STAGE WORKFLOW
# ==============================================================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    txt = (
        "🏛️ **CA Tracker Pro (UPSC CSE 2027) Engine Active!**\n\n"
        "Rozana newspaper PDF ya article text yahan forward karein.\n\n"
        "**Workflow:**\n"
        "1️⃣ **Stage 1 (Filter):** Newspaper se 4–6 non-political high-yield editorials aur 3 PIB releases ki 15-Year PYQ Linked list milegi.\n"
        "2️⃣ **Stage 2 (Selection & Upload):** Aap jo numbers chunenge, unka AIR-1 standard Hindi analysis Notion database me automatically upload ho jayega."
    )
    bot.reply_to(message, txt, parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_pdf_document(message):
    if not message.document.file_name.lower().endswith('.pdf'):
        bot.reply_to(message, "⚠️ Kripya sirf valid newspaper PDF file bhejein.")
        return

    status_msg = bot.reply_to(message, "⏳ Newspaper download ho raha hai...")
    temp_file = f"temp_{message.chat.id}_{message.document.file_name}"

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(temp_file, 'wb') as f:
            f.write(downloaded)

        bot.edit_message_text(
            "🧠 Gemini 1.5 Flash se PDF analyze ho raha hai... (Stage 1 Filter List ban rahi hai)",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

        g_file = genai.upload_file(temp_file, mime_type="application/pdf")
        process_stage1_content(message, g_file, is_file=True, status_msg_id=status_msg.message_id)

    except Exception as e:
        bot.edit_message_text(f"❌ Error: {str(e)[:200]}", chat_id=message.chat.id, message_id=status_msg.message_id)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

@bot.message_handler(content_types=['text'])
def handle_text_messages(message):
    # Agar user Stage-1 selection ka reply de raha hai
    if message.chat.id in USER_SESSIONS and not message.text.startswith('/'):
        handle_stage2_execution(message)
        return

    # Agar user ne bada text ya editorial paste kiya hai
    if len(message.text.strip()) > 300:
        status_msg = bot.reply_to(message, "🧠 Article text analyze ho raha hai... (Stage 1 List taiyar ho rahi hai)")
        try:
            process_stage1_content(message, message.text, is_file=False, status_msg_id=status_msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"❌ Error: {str(e)[:200]}", chat_id=message.chat.id, message_id=status_msg.message_id)

def process_stage1_content(message, content_source, is_file, status_msg_id):
    """Stage 1: Extracts candidate articles and 3 PIB releases"""
    stage1_prompt = """
    Aap UPSC Civil Services Examination ke top mentor aur evaluator hain.
    Diye gaye newspaper content se sirf wahi articles extract karein jo UPSC syllabus ke anuroop hon.

    STRICT FILTER RULES:
    1. ZERO political sparring, party statements, routine crimes, or superficial appointments without policy substance.
    2. Every item must be mapped to last 15-year Prelims or Mains PYQ trend.
    3. At least 1 item must be MANDATORY 'Prelims-Only' (micro-facts, schemes, ecology/science facts).
    4. Provide exactly 3 high-yield PIB recommendations relevant to today's governance & policy.

    Output STRICTLY a JSON object matching this schema:
    {
      "newspaper_articles": [
        {
          "id": 1,
          "title": "Concise English Title (for easy search)",
          "source": "The Hindu / Indian Express / IE Explained / Other",
          "gs_paper": "GS1 / GS2 / GS3 / GS4 / Prelims-Only",
          "relevance": ["Mains", "Prelims"],
          "topic": "Social Justice / Governance / Economy / Environment & Ecology / Science & Technology / IR",
          "sub_topic": "Specific domain (Health, Agriculture, Space, etc.)",
          "pyq_match": "Exact 15-year matched PYQ with year (e.g., Prelims 2021: CRISPR / Mains 2023 GS2: SHGs)",
          "summary": "1 line core administrative issue"
        }
      ],
      "pib_recommendations": [
        {
          "id": "PIB-1",
          "title": "English Title of PIB Policy / Scheme Release",
          "ministry": "Concerned Ministry / Department",
          "gs_paper": "GS Paper",
          "topic": "Broad topic",
          "sub_topic": "Specific domain",
          "pyq_match": "Related PYQ reference",
          "summary": "1 line policy / scheme context"
        }
      ]
    }
    Provide 4 to 6 top newspaper candidates and exactly 3 PIB candidates.
    """

    json_model = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={"response_mime_type": "application/json", "temperature": 0.2}
    )

    inputs = [content_source, stage1_prompt] if is_file else [stage1_prompt + f"\n\nContent:\n{content_source[:30000]}"]
    resp = json_model.generate_content(inputs)
    parsed_data = json.loads(clean_json_response(resp.text))

    USER_SESSIONS[message.chat.id] = {
        "content_source": content_source,
        "is_file": is_file,
        "stage1_data": parsed_data
    }

    out_text = "📋 **STAGE 1: UPSC Candidate Articles Selection**\n\n"
    out_text += "🗞️ **Newspaper Editorials & Articles:**\n"
    for a in parsed_data.get("newspaper_articles", []):
        rel_str = "/".join(a.get("relevance", ["Mains"]))
        out_text += f"**[{a['id']}]** {a['title']}\n"
        out_text += f"   • Source: `{a['source']}` | {a['gs_paper']} ({rel_str})\n"
        out_text += f"   • Topic: {a['topic']} > {a['sub_topic']}\n"
        out_text += f"   • 15-Yr PYQ: `{a['pyq_match']}`\n\n"

    out_text += "🏛️ **PIB Press Releases (Mandatory 3):**\n"
    for p in parsed_data.get("pib_recommendations", []):
        out_text += f"**[{p['id']}]** {p['title']}\n"
        out_text += f"   • `{p['ministry']}` | {p['gs_paper']}\n"
        out_text += f"   • PYQ: `{p['pyq_match']}`\n\n"

    out_text += "👉 **Aap kaunse articles Notion me upload karna chahte hain?**\nReply karein jaise: `1, 2, 4, PIB-1`"

    try:
        bot.edit_message_text(out_text, chat_id=message.chat.id, message_id=status_msg_id, parse_mode="Markdown")
    except Exception:
        safe_send_message(message.chat.id, out_text, parse_mode="Markdown")

def handle_stage2_execution(message):
    """Stage 2: Deep content generation and Notion upload"""
    session = USER_SESSIONS[message.chat.id]
    user_choices = [c.strip().upper() for c in message.text.replace(" ", "").split(",") if c.strip()]

    status_msg = bot.reply_to(message, "⚙️ Selected articles ka deep analytical content build ho raha hai aur Notion me upload kiya ja raha hai...")

    try:
        stage1_data = session["stage1_data"]
        candidates = stage1_data.get("newspaper_articles", []) + stage1_data.get("pib_recommendations", [])
        selected_candidates = [c for c in candidates if str(c.get("id")).upper() in user_choices]

        if not selected_candidates:
            bot.reply_to(message, "⚠️ Diye gaye numbers match nahi huye. Kripya `1, 3, PIB-1` ke format me bhejein.")
            return

        stage2_prompt = f"""
        Aap UPSC Civil Services Examination AIR-1 rank standard content creator hain.
        Neeche diye gaye selected items ka full-length, exam-usable shuddh prashasnik Hindi content generate karein:
        {json.dumps(selected_candidates, ensure_ascii=False)}

        STRICT SPECIFICATIONS FOR EACH ARTICLE:
        1. PRELIMS-SPECIFIC ARTICLES:
           - Word Count: Minimum 200+ words.
           - Quality: Dense with micro-facts, nodal ministries, constitutional articles, statutory/constitutional status, scientific mechanisms.
           - PYQ Matrix: Last 15-year Prelims PYQ match and factual hooks.
           - Color Coding: Use {{{{blue:term}}}}, {{{{green:data}}}}, {{{{orange:fact}}}}, {{{{purple:scheme}}}}, {{{{pink:status}}}} across the text.
           - Sub-keywords: Provide 2 to 5 specific tags in "topic_tags".

        2. MAINS-SPECIFIC ARTICLES:
           - Word Count: Minimum 350 to 400+ words (high analytical density).
           - Headings Structure:
             ## संदर्भ एवं पृष्ठभूमि
             ## मुख्य बिंदु एवं नीतिगत आयाम
             ## मुख्य परीक्षा मूल्य संवर्धन (Data, Committee reports, Supreme Court judgments)
             ## आगे की राह (Actionable administrative recommendations)
           - Color Coding: Multiple colors using {{{{color:term}}}} syntax for all crucial terms/data.
           - Taglines & administrative terms directly usable in answer writing.

        3. DEDICATED FIELDS (Must be top quality):
           - "quote_hindi": "[Dharshnik/Chintak]: '[Deep ethical/philosophical quote for Essay and GS-4]'"
           - "ethics_issue": "मुद्दा: [Concise administrative ethical dilemma / example]"
           - "philosophy_issue": "मुद्दा: [Rawlsian justice / Kantian deontology / Utilitarianism / Indian philosophy angle]"
           - "mains_question": Tough, application-based question (only for top 2 analytical articles, null for others).
           - "mains_framework": Clear "भूमिका - मुख्य भाग - निष्कर्ष" structure for those 2 questions.

        Output STRICTLY a valid JSON array of objects:
        [
          {{
            "article_title": "Concise English Title",
            "source": "The Hindu / Indian Express / IE Explained / PIB",
            "priority": "High / Medium",
            "gs_paper": "GS Paper",
            "relevance": ["Mains", "Prelims"],
            "topic": "Broad Topic",
            "sub_topic": "Specific Domain",
            "topic_tags": ["Tag1", "Tag2", "Tag3"],
            "pyq_reference": "Exact 15-year matched PYQ with year and question text",
            "quote_hindi": "Chintak: 'Quote'",
            "ethics_issue": "Ethics Angle",
            "philosophy_issue": "Philosophy Angle",
            "mains_question": "Question text or null",
            "mains_framework": "Answer framework or null",
            "source_link": "https://thehindu.com",
            "crux_hindi": "Full markdown text with {{{{color:text}}}} annotations"
          }}
        ]
        """

        json_model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={"response_mime_type": "application/json", "temperature": 0.2}
        )

        inputs = [session["content_source"], stage2_prompt] if session["is_file"] else [stage2_prompt + f"\n\nContent:\n{session['content_source'][:30000]}"]
        resp = json_model.generate_content(inputs)
        full_articles = json.loads(clean_json_response(resp.text))

        bot.edit_message_text(
            f"📤 {len(full_articles)} articles Notion database me upload aur verify ho rahe hain...",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

        success_entries = []
        failed_entries = []

        for art in full_articles:
            ok, err_msg = create_notion_entry(art)
            if ok:
                success_entries.append(f"• **{art.get('article_title')}** ({art.get('gs_paper')})")
            else:
                failed_entries.append(f"• **{art.get('article_title')}** (Reason: {err_msg})")

        del USER_SESSIONS[message.chat.id]

        report = f"🎯 **Notion Sync & Verification Report**\n\n"
        report += f"📊 Total: {len(full_articles)} | Success: {len(success_entries)} | Failed: {len(failed_entries)}\n\n"
        if success_entries:
            report += "✅ **Successfully Uploaded:**\n" + "\n".join(success_entries) + "\n\n"
        if failed_entries:
            report += "⚠️ **Errors:**\n" + "\n".join(failed_entries)

        safe_send_message(message.chat.id, report, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"❌ Stage 2 Execution Error: {str(e)[:250]}", chat_id=message.chat.id, message_id=status_msg.message_id)

# ==============================================================================
# 5. EXECUTION ENTRY POINT
# ==============================================================================
def run_flask_app():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask_app, daemon=True).start()
    print("=" * 60)
    print("🚀 CA Tracker Pro Bot started polling successfully!")
    print("=" * 60)
    bot.infinity_polling(timeout=20, long_polling_timeout=15)

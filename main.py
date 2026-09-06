import os
import re
import json
import datetime
import threading
import requests
from flask import Flask
import telebot
import google.generativeai as genai

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("DATABASE_ID", "1ece01bb658c4f2aa38398d771127eed")

if not all([TELEGRAM_TOKEN, GEMINI_KEY, NOTION_TOKEN]):
    raise ValueError("Missing critical environment variables: TELEGRAM_TOKEN, GEMINI_KEY, or NOTION_TOKEN")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_KEY)

# Flask Server (Render port binding ke liye)
app = Flask(__name__)

@app.route('/')
def health_check():
    return "CA Tracker Pro 24/7 Engine is Running!"

# User multi-step sessions store karne ke liye
USER_SESSIONS = {}

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

# ==========================================
# 2. NOTION SCHEMA & BLOCK UTILITIES
# ==========================================
def fetch_database_schema():
    """Notion database ke active properties dynamically fetch karta hai"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}"
    try:
        res = requests.get(url, headers=NOTION_HEADERS, timeout=15)
        if res.status_code == 200:
            return res.json().get("properties", {})
    except Exception as err:
        print(f"Schema Fetch Error: {err}")
    return {}

DB_PROPS = fetch_database_schema()

def clean_json_string(raw_text):
    """Gemini markdown wrap ko strip karke pure JSON banata hai"""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()

def build_colored_notion_blocks(markdown_text):
    """
    Keywords ko multi-color Notion text annotations me convert karta hai.
    Format in text: {{color:keyword}} -> e.g., {{blue:NITI Aayog}}, {{green:Article 21}}
    Palette: blue, green, orange, purple, pink, red, yellow, brown
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
        
        # Initial text before first keyword
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

def append_blocks_safely(page_id, blocks):
    """Notion API ke 100 blocks limit ko bypass karne ke liye chunking method"""
    url = f"[https://api.notion.com/v1/blocks/](https://api.notion.com/v1/blocks/){page_id}/children"
    for i in range(0, len(blocks), 80):
        batch = blocks[i:i+80]
        requests.patch(url, headers=NOTION_HEADERS, json={"children": batch}, timeout=15)

def push_article_to_notion(art):
    """Article metadata aur content ko exact schema properties ke anuroop create karta hai"""
    global DB_PROPS
    if not DB_PROPS:
        DB_PROPS = fetch_database_schema()

    props = {}

    # Helper lambda to match column names case-insensitively
    def find_prop(*possible_names):
        for name in DB_PROPS.keys():
            if any(p.lower() == name.lower() or p.lower() in name.lower() for p in possible_names):
                return name
        return None

    # 1. Article Title (Title property)
    title_col = find_prop("Article Title", "Name", "Title") or "Article Title"
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
        props[prio_col] = {"select": {"name": art.get("priority")}}

    # 4. GS Paper
    gs_col = find_prop("GS Paper", "GS")
    if gs_col and art.get("gs_paper"):
        props[gs_col] = {"select": {"name": art.get("gs_paper")}}

    # 5. Source
    src_col = find_prop("Source")
    if src_col and art.get("source"):
        props[src_col] = {"select": {"name": art.get("source")}}

    # 6. Topic
    top_col = find_prop("Topic")
    if top_col and art.get("topic"):
        props[top_col] = {"select": {"name": art.get("topic")}}

    # 7. Sub-Topic
    sub_col = find_prop("Sub-Topic", "Sub Topic")
    if sub_col and art.get("sub_topic"):
        p_type = DB_PROPS[sub_col].get("type")
        if p_type == "select":
            props[sub_col] = {"select": {"name": art.get("sub_topic")}}
        else:
            props[sub_col] = {"rich_text": [{"text": {"content": art.get("sub_topic")[:1900]}}]}

    # 8. Relevance (Multi-select)
    rel_col = find_prop("Relevance")
    if rel_col and art.get("relevance"):
        rel_vals = art.get("relevance") if isinstance(art.get("relevance"), list) else [art.get("relevance")]
        props[rel_col] = {"multi_select": [{"name": str(r)} for r in rel_vals]}

    # 9. PYQ Reference
    pyq_col = find_prop("PYQ Reference", "PYQ")
    if pyq_col and art.get("pyq_reference"):
        props[pyq_col] = {"rich_text": [{"text": {"content": str(art.get("pyq_reference"))[:1900]}}]}

    # 10. Ethics Issue-Example (Hindi)
    eth_col = find_prop("Ethics Issue-Example", "Ethics")
    if eth_col and art.get("ethics_issue"):
        props[eth_col] = {"rich_text": [{"text": {"content": str(art.get("ethics_issue"))[:1900]}}]}

    # 11. Philosophy Issue-Example (Hindi)
    phil_col = find_prop("Philosophy Issue-Example", "Philosophy")
    if phil_col and art.get("philosophy_issue"):
        props[phil_col] = {"rich_text": [{"text": {"content": str(art.get("philosophy_issue"))[:1900]}}]}

    # 12. Quote (Hindi)
    quote_col = find_prop("Quote (Hindi)", "Quote")
    if quote_col and art.get("quote_hindi"):
        props[quote_col] = {"rich_text": [{"text": {"content": str(art.get("quote_hindi"))[:1900]}}]}

    # 13. Mains Question (Hindi)
    mq_col = find_prop("Mains Question")
    if mq_col and art.get("mains_question"):
        props[mq_col] = {"rich_text": [{"text": {"content": str(art.get("mains_question"))[:1900]}}]}

    # 14. Mains Answer Framework (Hindi)
    mf_col = find_prop("Mains Answer Framework", "Framework")
    if mf_col and art.get("mains_framework"):
        props[mf_col] = {"rich_text": [{"text": {"content": str(art.get("mains_framework"))[:1900]}}]}

    # 15. Date & Source Link
    date_col = find_prop("date", "Date")
    if date_col:
        props[date_col] = {"date": {"start": datetime.date.today().isoformat()}}

    link_col = find_prop("Source Link", "URL", "Link")
    if link_col and art.get("source_link"):
        props[link_col] = {"url": art.get("source_link")}

    # Initial blocks (first 80)
    all_blocks = build_colored_notion_blocks(art.get("crux_hindi", ""))
    initial_blocks = all_blocks[:80]
    extra_blocks = all_blocks[80:]

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": props,
        "children": initial_blocks
    }

    url = "[https://api.notion.com/v1/pages](https://api.notion.com/v1/pages)"
    res = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=20)
    
    if res.status_code == 200:
        page_id = res.json().get("id")
        if extra_blocks:
            append_blocks_safely(page_id, extra_blocks)
        return True, "Success"
    else:
        return False, res.text[:200]

# ==========================================
# 3. TELEGRAM BOT WORKFLOW (2-STAGE SYSTEM)
# ==========================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    welcome_text = (
        "🏛️ **CA Tracker Pro Engine Active!**\n\n"
        "Newspaper PDF forward karein. Main do-charano me process karunga:\n"
        "1. **Stage 1 (Filter):** Non-political curated Editorials + 3 PIB Releases ki 15-Year PYQ Linked List milegi.\n"
        "2. **Stage 2 (Upload):** Aapke selection ke aadhar par deep analytical content Notion me upload hoga."
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_pdf_upload(message):
    if not message.document.file_name.lower().endswith('.pdf'):
        bot.reply_to(message, "⚠️ Kripya sirf valid newspaper PDF file bhejein.")
        return

    status_msg = bot.reply_to(message, "⏳ Newspaper PDF download ho raha hai...")
    temp_file = f"temp_{message.document.file_name}"

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(temp_file, 'wb') as f:
            f.write(downloaded)

        bot.edit_message_text(
            "🧠 Gemini 1.5 Flash se PDF analyze ho raha hai... (Stage 1 Selection List taiyar ki ja rahi hai)",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

        g_file = genai.upload_file(temp_file, mime_type="application/pdf")

        stage1_prompt = """
        Aap UPSC CSE ke top evaluator aur mentor hain. Is newspaper PDF se kewal exam-relevant high-yield issues filter karein.
        STRICT RULES:
        - ZERO political sparring/gossip, appointments without administrative substance.
        - Link each with last 15-year Prelims or Mains PYQ trend.
        - Ensure at least 1 mandatory Prelims-specific item.
        - Generate exactly 3 relevant PIB releases for the current date's governance context.

        Return strictly a valid JSON object matching this structure:
        {
          "newspaper_articles": [
            {
              "id": 1,
              "title": "Concise English Title",
              "source": "The Hindu / Indian Express / IE Explained",
              "gs_paper": "GS1 / GS2 / GS3 / GS4 / Prelims-Only",
              "relevance": "Prelims-Specific / Mains-Specific / Both",
              "topic": "Social Justice / Governance / Science & Technology / Environment & Ecology / Economy / IR",
              "sub_topic": "Specific domain",
              "pyq_match": "Exact 15-year micro-topic match (e.g., Prelims 2021: CRISPR / Mains 2023 GS2: SHGs)",
              "summary": "1 line core administrative issue"
            }
          ],
          "pib_recommendations": [
            {
              "id": "PIB-1",
              "title": "Concise English Title of Policy Release",
              "ministry": "Concerned Ministry / Department",
              "gs_paper": "GS Paper",
              "topic": "Broad syllabus topic",
              "sub_topic": "Specific domain",
              "pyq_match": "Related PYQ reference",
              "summary": "1 line scheme or policy context"
            }
          ]
        }
        Provide 4 to 6 top newspaper candidates and exactly 3 PIB candidates.
        """

        json_model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        resp = json_model.generate_content([g_file, stage1_prompt])
        parsed_data = json.loads(clean_json_string(resp.text))

        # Save session
        USER_SESSIONS[message.chat.id] = {
            "gemini_file": g_file,
            "stage1_data": parsed_data
        }

        # Build Stage 1 Message
        out_msg = "📋 **STAGE 1: Candidate Current Affairs Selection**\n\n"
        out_msg += "🗞️ **Newspaper Articles & Editorials:**\n"
        for a in parsed_data.get("newspaper_articles", []):
            out_msg += f"**[{a['id']}]** {a['title']}\n"
            out_msg += f"   • Source: `{a['source']}` | {a['gs_paper']} ({a['relevance']})\n"
            out_msg += f"   • Topic: {a['topic']} > {a['sub_topic']}\n"
            out_msg += f"   • 15-Yr PYQ: `{a['pyq_match']}`\n\n"

        out_msg += "🏛️ **PIB Recommendations (Mandatory 3):**\n"
        for p in parsed_data.get("pib_recommendations", []):
            out_msg += f"**[{p['id']}]** {p['title']}\n"
            out_msg += f"   • `{p['ministry']}` | {p['gs_paper']}\n"
            out_msg += f"   • PYQ: `{p['pyq_match']}`\n\n"

        out_msg += "👉 **Aap kaunse articles Notion me upload karna chahte hain?**\nReply karein jaise: `1, 2, 4, PIB-1`"

        bot.edit_message_text(out_msg, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"❌ Stage 1 Error: {str(e)[:200]}", chat_id=message.chat.id, message_id=status_msg.message_id)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

@bot.message_handler(func=lambda m: m.chat.id in USER_SESSIONS and m.text and not m.text.startswith('/'))
def handle_stage2_selection(message):
    session = USER_SESSIONS[message.chat.id]
    user_choices = [c.strip().upper() for c in message.text.replace(" ", "").split(",")]

    status_msg = bot.reply_to(message, "⚙️ Deep analytical content build ho raha hai aur Notion me upload kiya ja raha hai...")

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

        STRICT INSTRUCTIONS FOR EACH ITEM:
        1. PRELIMS-SPECIFIC ARTICLES:
           - Word Count: Minimum 200+ words.
           - Focus: 15-year Prelims PYQ matrix, factual precision, nodal ministries, constitutional articles, scientific mechanisms, micro-facts.
           - Color Coding: Multiple colors using {{{{color:term}}}} syntax (available colors: blue, green, orange, purple, pink).
           - Structure: Small headings (## संदर्भ, ## मुख्य प्रारंभिक तथ्य, ## विगत वर्ष प्रश्न जुड़ाव).

        2. MAINS-SPECIFIC ARTICLES:
           - Word Count: Minimum 350 to 400+ words (dense with terminology).
           - Structure:
             ## संदर्भ एवं पृष्ठभूमि
             ## मुख्य बिंदु एवं नीतिगत आयाम
             ## मुख्य परीक्षा मूल्य संवर्धन (Data, Committee reports, Supreme Court judgments)
             ## आगे की राह (Administrative actionable way forward)
           - Color Coding: Multiple colors using {{{{blue:term}}}}, {{{{green:data}}}}, {{{{orange:committee}}}}, {{{{purple:concept}}}}.
           - Taglines & terminology directly usable in answer writing.

        3. DEDICATED FIELDS (Must be high standard):
           - "quote_hindi": "[Thinker]: '[Deep philosophical/ethical quote for Essay/GS4]'"
           - "ethics_issue": "मुद्दा: [Concise administrative ethical dilemma / example]"
           - "philosophy_issue": "मुद्दा: [Rawlsian justice / Kantian ethics / Utilitarianism / Indian darshan angle]"
           - "mains_question": Generate for the top 2 analytical articles (Tough application-based question).
           - "mains_framework": Clear "भूमिका - मुख्य भाग - निष्कर्ष" structure for those 2 questions.

        Return strictly a valid JSON array of objects:
        [
          {{
            "article_title": "English Title",
            "source": "Source Name",
            "priority": "High / Medium",
            "gs_paper": "GS Paper",
            "relevance": ["Mains", "Prelims"],
            "topic": "Broad Topic",
            "sub_topic": "Specific Topic",
            "pyq_reference": "Exact 15-year matched PYQ with year and text",
            "quote_hindi": "Chintak: 'Quote'",
            "ethics_issue": "Ethics Angle",
            "philosophy_issue": "Philosophy Angle",
            "mains_question": "Question text or null",
            "mains_framework": "Answer framework or null",
            "source_link": "[https://thehindu.com](https://thehindu.com)",
            "crux_hindi": "Full markdown body with {{{{color:text}}}} tags"
          }}
        ]
        """

        json_model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        resp = json_model.generate_content([session["gemini_file"], stage2_prompt])
        full_articles = json.loads(clean_json_string(resp.text))

        bot.edit_message_text(
            f"📤 {len(full_articles)} articles Notion database me upload ho rahe hain aur verify kiye ja rahe hain...",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

        success_entries = []
        failed_entries = []

        for art in full_articles:
            ok, msg = push_article_to_notion(art)
            if ok:
                success_entries.append(f"• {art.get('article_title')} ({art.get('gs_paper')})")
            else:
                failed_entries.append(f"• {art.get('article_title')} (Reason: {msg})")

        # Clear session
        del USER_SESSIONS[message.chat.id]

        report = f"✅ **Notion Sync Report ({len(success_entries)}/{len(full_articles)} Uploaded)**\n\n"
        if success_entries:
            report += "📌 **Successfully Verified:**\n" + "\n".join(success_entries) + "\n\n"
        if failed_entries:
            report += "⚠️ **Errors:**\n" + "\n".join(failed_entries)

        bot.edit_message_text(report, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"❌ Stage 2 Execution Error: {str(e)[:250]}", chat_id=message.chat.id, message_id=status_msg.message_id)

def start_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Background thread for Render keep-alive
    threading.Thread(target=start_flask, daemon=True).start()
    print("Bot polling started...")
    bot.infinity_polling(timeout=15, long_polling_timeout=10)

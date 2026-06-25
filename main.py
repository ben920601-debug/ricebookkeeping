import os
import re
import json
import random
import httpx
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional
from fastapi.middleware.cors import CORSMiddleware

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI
from google import genai
from google.genai import types

from dotenv import load_dotenv
load_dotenv()

app = FastAPI(title="記帳米粒 ｜ SQLite 輕量極速版")

# 🎯 加入 CORS 跨域設定，允許您的 LIFF 網頁來抓取 API 資料
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允許所有網域請求
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# SQLite 檔案路徑
DB_PATH = "ricebook.db"

def get_db_connection():
    """取得資料庫連線，並設定 row_factory 方便以字典方式存取"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化 SQLite 資料表 (對應原本 Firebase 的各個 Collection)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 群組與狀態表 (替代 groups collection)
    cursor.execute('''CREATE TABLE IF NOT EXISTS groups (
        target_id TEXT PRIMARY KEY,
        state TEXT DEFAULT 'normal',
        active_order_code TEXT DEFAULT '',
        order_items_temp TEXT DEFAULT '[]',
        created_at DATETIME
    )''')
    
    # 群組成員快取表 (替代 members collection)
    cursor.execute('''CREATE TABLE IF NOT EXISTS members (
        target_id TEXT,
        user_id TEXT,
        display_name TEXT,
        updated_at DATETIME,
        PRIMARY KEY (target_id, user_id)
    )''')
    
    # 記帳花費表 (替代 expenses collection)
    cursor.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT,
        type TEXT,
        amount INTEGER,
        item TEXT,
        category TEXT,
        timestamp DATETIME,
        created_by_uid TEXT,
        created_by_name TEXT
    )''')
    
    # 團購訂單表 (替代 orders collection)
    cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        target_id TEXT,
        order_date TEXT,
        order_code TEXT,
        total_amount INTEGER,
        master_payer_id TEXT,
        master_payer_name TEXT,
        items TEXT, 
        timestamp DATETIME
    )''')
    
    # 核銷紀錄表 (替代 settlements collection)
    cursor.execute('''CREATE TABLE IF NOT EXISTS settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT,
        payer_id TEXT,
        receiver_id TEXT,
        payer_name TEXT,
        receiver_name TEXT,
        amount INTEGER,
        order_code_ref TEXT,
        timestamp DATETIME
    )''')
    
    conn.commit()
    conn.close()
    print("🔥 [DATABASE] SQLite 本地資料庫就位！", flush=True)

# 啟動時建立資料表
init_db()

# ==========================================
# 🛡️ 2. 全域型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")

class SingleSettlement(BaseModel):
    payer_name: str = Field(default="")
    receiver_name: str = Field(default="")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(default="")
    item_name: str = Field(default="")
    price: int = Field(default=0)

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "order_start", "order_end", "order_item", "settle_start", "settle_pay", "settle_end"] = Field(
        description="核心意圖分流"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    ai_reply: Optional[str] = Field(default="", description="與使用者的聊天回應")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def send_line_reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        print(f"❌ LINE 回覆失敗: {e}", flush=True)

def get_real_mentions(event) -> list:
    """🎯 核心修復：過濾掉機器人自身的 Tag，只抓取真實成員的 ID"""
    real_tagged_ids = []
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees:
        text = getattr(event.message, "text", "")
        for m in mention.mentionees:
            u_id = getattr(m, "user_id", None)
            if u_id:
                try:
                    tagged_text = text[m.index : m.index + m.length]
                    if "米粒" in tagged_text:
                        continue
                except:
                    pass
                real_tagged_ids.append(u_id)
    return real_tagged_ids

def fetch_line_profile_name(user_id: str, target_id: str = None) -> str:
    """🎯 核心修復：升級為群組成員 API，未加好友也能抓到真實暱稱"""
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    
    if target_id and target_id.startswith("C"):
        url = f"https://api.line.me/v2/bot/group/{target_id}/member/{user_id}"
        try:
            res = httpx.get(url, headers=headers, timeout=5.0)
            if res.status_code == 200:
                return res.json().get("displayName", f"成員({user_id[:4]})")
        except Exception:
            pass
            
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            return res.json().get("displayName", f"成員({user_id[:4]})")
    except Exception:
        pass
        
    return f"成員({user_id[:4]})"

def resolve_id_to_name(target_id: str, user_id: str) -> str:
    if not user_id: return "群組夥伴"
    if not user_id.startswith("U"): return user_id
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT display_name FROM members WHERE target_id = ? AND user_id = ?", (target_id, user_id))
    row = cursor.fetchone()
    
    if row:
        conn.close()
        return row['display_name']
    else:
        real_name = fetch_line_profile_name(user_id, target_id)
        # 寫入或更新成員表
        cursor.execute('''
            INSERT INTO members (target_id, user_id, display_name, updated_at) 
            VALUES (?, ?, ?, ?) 
            ON CONFLICT(target_id, user_id) DO UPDATE SET 
            display_name=excluded.display_name, updated_at=excluded.updated_at
        ''', (target_id, user_id, real_name, datetime.utcnow()))
        conn.commit()
        conn.close()
        return real_name

# ==========================================
# 🌐 4. Webhook 核心主線
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: line_handler.handle(body_str, signature)
    except InvalidSignatureError: pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    reply_token = event.reply_token 
    target_id = event.source.group_id if is_group else creator_id

    # 開啟資料庫連線
    conn = get_db_connection()
    cursor = conn.cursor()

    current_mode = "normal"
    active_code = ""
    
    if is_group:
        cursor.execute("SELECT state, active_order_code FROM groups WHERE target_id = ?", (target_id,))
        row = cursor.fetchone()
        if row:
            current_mode = row['state']
            active_code = row['active_order_code']
        else:
            # 如果群組不存在，建立預設資料
            cursor.execute('''
                INSERT INTO groups (target_id, state, active_order_code, order_items_temp, created_at) 
                VALUES (?, 'normal', '', '[]', ?)
            ''', (target_id, datetime.utcnow()))
            conn.commit()

    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@記帳米粒", "記帳米粒"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: 
        conn.close()
        return 

    # ====================================================
    # 🎯 🛠️ 【核銷解鎖與防呆邏輯】
    # ====================================================
    is_settle_trigger = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    if is_group and current_mode == "normal" and is_settle_trigger:
        code_match = re.search(r'#?(\d{4})', user_text)
        if not code_match:
            send_line_reply(reply_token, "⚠️ 必須輸入對應的 4 位數團購單號才可開啟核銷模式。\n👉 範例：『@記帳米粒 申請核銷 #1234』")
            conn.close()
            return
            
        req_code = code_match.group(1)
        cursor.execute("SELECT master_payer_id FROM orders WHERE target_id = ? AND order_code = ?", (target_id, req_code))
        order_found = cursor.fetchone()
            
        if not order_found:
            send_line_reply(reply_token, f"❌ 找不到本群組內編號為 #{req_code} 的團購單。")
            conn.close()
            return
            
        cursor.execute("UPDATE groups SET state='settle', active_order_code=? WHERE target_id=?", (req_code, target_id))
        conn.commit()
        
        payer_str = resolve_id_to_name(target_id, order_found['master_payer_id'] if order_found['master_payer_id'] else creator_id)
        send_line_reply(reply_token, f"🔓 成功解鎖結算模式！鎖定單號：#{req_code}\n💳 墊款買單人：{payer_str}\n👉 請開始核銷對帳（如：@記帳米粒 我核銷我自己 150）")
        conn.close()
        return

    # ====================================================
    # 🎯 🛠️ 【結算模式：互相核銷與自行核銷】
    # ====================================================
    if is_group and current_mode == "settle":
        if any(k in user_text for k in ["結算結束", "關閉結算", "退出結算", "核銷完畢"]):
            cursor.execute("UPDATE groups SET state='normal', active_order_code='' WHERE target_id=?", (target_id,))
            conn.commit()
            send_line_reply(reply_token, "🔓 結算完畢！已安全關閉對帳並恢復常態模式。")
            conn.close()
            return

        if any(k in user_text for k in ["給", "還", "付", "收", "核銷"]):
            clean_text = re.sub(r'#?\d{4}', '', user_text)
            amount_match = re.search(r'\d+', clean_text)
            settle_amount = int(amount_match.group()) if amount_match else 0
            if settle_amount <= 0: 
                conn.close()
                return

            real_tagged_ids = get_real_mentions(event)

            if len(real_tagged_ids) >= 2:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = real_tagged_ids[1]
            elif len(real_tagged_ids) == 1:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = creator_id
            else:
                final_payer_id = creator_id
                final_receiver_id = creator_id
                
            if final_payer_id and final_receiver_id:
                cursor.execute("SELECT items FROM orders WHERE target_id = ? AND order_code = ?", (target_id, active_code))
                current_order = cursor.fetchone()
                if not current_order: 
                    conn.close()
                    return
                
                # 計算應付款
                items_data = json.loads(current_order['items'])
                payer_expected_total = sum(item.get("price", 0) for item in items_data if item.get("buyer_id") == final_payer_id or item.get("buyer") == final_payer_id)
                
                # 計算已付款
                cursor.execute("SELECT SUM(amount) as total_paid FROM settlements WHERE target_id = ? AND order_code_ref = ? AND payer_id = ?", (target_id, active_code, final_payer_id))
                paid_res = cursor.fetchone()
                payer_already_paid = paid_res['total_paid'] if paid_res['total_paid'] else 0
                
                remaining_debt = payer_expected_total - payer_already_paid
                
                if remaining_debt <= 0:
                    send_line_reply(reply_token, f"❌ 登記拒絕！成員 {resolve_id_to_name(target_id, final_payer_id)} 在單號 #{active_code} 中並無欠款紀錄。")
                    conn.close()
                    return
                elif settle_amount > remaining_debt:
                    send_line_reply(reply_token, f"❌ 入帳失敗！金額溢繳！\n⚠️ 該成員此單賸餘應付為：${remaining_debt} 元，您輸入的 ${settle_amount} 元不符合規範，拒絕入帳。")
                    conn.close()
                    return
                
                payer_name_str = resolve_id_to_name(target_id, final_payer_id)
                receiver_name_str = resolve_id_to_name(target_id, final_receiver_id)

                cursor.execute('''
                    INSERT INTO settlements (target_id, payer_id, receiver_id, payer_name, receiver_name, amount, order_code_ref, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (target_id, final_payer_id, final_receiver_id, payer_name_str, receiver_name_str, settle_amount, active_code, datetime.utcnow()))
                conn.commit()

                if final_payer_id == final_receiver_id:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n🙋‍♂️ 自行核銷：{payer_name_str}\n💰 紀錄金額：${settle_amount}")
                else:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n💸 付款：{payer_name_str}\n📥 收款：{receiver_name_str}\n💰 紀錄金額：${settle_amount}")
                
                conn.close()
                return

    clean_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    # ====================================================
    # 📖 【Python 層攔截：系統說明書與報表派發】
    # ====================================================
    if any(k in clean_text for k in ["報表", "查帳", "大後台", "網址", "入口", "登入"]) and current_mode == "normal":
        send_line_reply(reply_token, f"📊 【記帳米粒 ｜ 雲端監控後台】\n🟢 入口如下：\nhttps://liff.line.me/{MY_LIFF_ID}?groupId={target_id}")
        conn.close()
        return

    if any(k in clean_text for k in ["使用說明", "怎麼用", "功能", "規定", "教學"]):
        instructions = (
            "📝 【記帳米粒 ｜ 使用說明書】\n"
            "-------------------------\n"
            "💡 「常態模式記帳」：\n"
            "👉 範例：『@記帳米粒 午餐 120』\n\n"
            "🛒 「團購模式：代點單」：\n"
            "👉 啟動：『@記帳米粒 開團』\n"
            "👉 自己點：『@記帳米粒 雞排 100』\n"
            "👉 幫人點：『@記帳米粒 @小明 珍奶 50』\n"
            "👉 結單：『@記帳米粒 結單』\n\n"
            "💳 「核銷模式：防呆平帳」：\n"
            "👉 啟動：『@記帳米粒 申請核銷 #單號』\n"
            "👉 代收：『@記帳米粒 @小明 給我 100』\n"
            "👉 自核：『@記帳米粒 我核銷 100』\n"
            "👉 關閉：『@記帳米粒 結算結束』"
        )
        send_line_reply(reply_token, instructions)
        conn.close()
        return

    for kw in SENSITIVE_KEYWORDS:
        if kw in clean_text:
            send_line_reply(reply_token, "🤖 米粒為純財務助理，請勿探討敏感議題喔！")
            conn.close()
            return

    # ====================================================
    # ⚡ 🚀 【Python 第一層極速攔截：代點單與記帳直通落庫】
    # ====================================================
    fast_match = re.fullmatch(r'^(.+?)\s*(\d+)\s*(?:元|塊)?$', clean_text)
    if fast_match and current_mode in ["normal", "order"]:
        raw_item_name = fast_match.group(1).strip()
        amount = int(fast_match.group(2))
        item_name = re.sub(r'@\S+', '', raw_item_name).strip()
        
        if not item_name.isdigit() and amount > 0:
            if current_mode == "normal":
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                cursor.execute('''
                    INSERT INTO expenses (target_id, type, amount, item, category, timestamp, created_by_uid, created_by_name)
                    VALUES (?, 'expense', ?, ?, '生活雜費', ?, ?, ?)
                ''', (target_id, amount, item_name, datetime.utcnow(), creator_id, creator_name_str))
                conn.commit()
                send_line_reply(reply_token, f"✅ 已紀錄：{item_name} ${amount}")
                conn.close()
                return
                
            elif current_mode == "order" and is_group:
                real_tagged_ids = get_real_mentions(event)
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                # 讀取並更新訂單暫存
                cursor.execute("SELECT order_items_temp FROM groups WHERE target_id = ?", (target_id,))
                row = cursor.fetchone()
                temp_items = json.loads(row['order_items_temp']) if row and row['order_items_temp'] else []
                temp_items.append({
                    "buyer_id": actual_buyer_id, "buyer": actual_buyer_name,
                    "item": item_name, "price": amount, "timestamp": datetime.utcnow().isoformat()
                })
                
                cursor.execute("UPDATE groups SET order_items_temp = ? WHERE target_id = ?", (json.dumps(temp_items, ensure_ascii=False), target_id))
                conn.commit()
                send_line_reply(reply_token, f"📝 已接單：{item_name} ${amount}")
                conn.close()
                return

    # ====================================================
    # 🧠 🧠 【第二層：Gemini 核心大腦 - 複雜萃取與自然陪聊】
    # ====================================================
    try:
        env_context = "群組" if is_group else "私訊"
        prompt = f"""
        你是一個親切、幽默的記帳助理「記帳米粒」。目前位於【{env_context}】環境，模式為【{current_mode}】。
        使用者輸入了：『{clean_text}』
        
        【分流任務】：
        1. 判定 intent (record, order_start, order_end, order_item, chat)。
        2. 如果對話中包含「花費與金額」（例如：今天買咖啡花了150元），請提取出紀錄 (intent="record")，並在 ai_reply 中給予親切的聊天回覆。
        3. 如果是純閒聊，intent="chat"，請在 ai_reply 陪使用者自然對話。
        4. 開團(order_start) 或 結單(order_end) 等控制指令，請在 ai_reply 給予親切的確認回覆。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.3),
        ).parsed

        # 1. AI 萃取記帳與陪聊
        if result.intent == "record":
            if result.records:
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                for rec in result.records:
                    if rec.amount > 0:
                        cursor.execute('''
                            INSERT INTO expenses (target_id, type, amount, item, category, timestamp, created_by_uid, created_by_name)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (target_id, rec.record_type, rec.amount, rec.item, rec.category, datetime.utcnow(), creator_id, creator_name_str))
                conn.commit()
                reply_text = result.ai_reply if result.ai_reply else f"✅ 已為您紀錄花費。"
                send_line_reply(reply_token, f"🤖 {reply_text}")

        # 2. 開團模式
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            cursor.execute("UPDATE groups SET state='order', active_order_code=?, order_items_temp='[]' WHERE target_id=?", (code_str, target_id))
            conn.commit()
            reply_text = result.ai_reply if result.ai_reply else f"🚀 【團購已啟動】本團單號：#{code_str}\n👉 請大家叫單時記得「@記帳米粒 品項 金額」喔！"
            send_line_reply(reply_token, reply_text)

        # 3. AI 萃取複雜點單與代點單
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                cursor.execute("SELECT order_items_temp FROM groups WHERE target_id = ?", (target_id,))
                row = cursor.fetchone()
                temp_items = json.loads(row['order_items_temp']) if row and row['order_items_temp'] else []
                
                real_tagged_ids = get_real_mentions(event)
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                reply_lines = []
                for item in result.order_items:
                    clean_item_name = re.sub(r'@\S+', '', item.item_name).strip()
                    temp_items.append({
                        "buyer_id": actual_buyer_id, "buyer": actual_buyer_name,
                        "item": clean_item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()
                    })
                    reply_lines.append(f"📝 已接單：{clean_item_name} ${item.price}")
                    
                cursor.execute("UPDATE groups SET order_items_temp=? WHERE target_id=?", (json.dumps(temp_items, ensure_ascii=False), target_id))
                conn.commit()
                send_line_reply(reply_token, "\n".join(reply_lines))

        # 4. 截止結單
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            cursor.execute("SELECT active_order_code, order_items_temp FROM groups WHERE target_id = ?", (target_id,))
            row = cursor.fetchone()
            active_code_val = row['active_order_code'] if row else ""
            temp_items = json.loads(row['order_items_temp']) if row and row['order_items_temp'] else []
            
            if temp_items:
                total_amt = sum(i["price"] for i in temp_items)
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                order_id = f"{datetime.now().strftime('%Y%m%d')}_{active_code_val}"
                
                cursor.execute('''
                    INSERT INTO orders (order_id, target_id, order_date, order_code, total_amount, master_payer_id, master_payer_name, items, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (order_id, target_id, datetime.now().strftime("%Y-%m-%d"), active_code_val, total_amt, creator_id, creator_name_str, json.dumps(temp_items, ensure_ascii=False), datetime.utcnow()))
                
                reply_text = result.ai_reply if result.ai_reply else f"🏁 【團購截止 ｜ 單號 #{active_code_val}】\n💰 總金額：${total_amt} 元\n💳 墊款：{creator_name_str}\n\n🤖 數據已更新！"
                send_line_reply(reply_token, reply_text)
            else:
                send_line_reply(reply_token, "🛑 因無人叫單，本團已直接關閉。")
                
            cursor.execute("UPDATE groups SET state='normal', order_items_temp='[]', active_order_code='' WHERE target_id=?", (target_id,))
            conn.commit()

        # 5. 純粹對話陪聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(reply_token, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 解析異常: {e}")
    finally:
        conn.close() # 確保最後關閉資料庫連線

@app.get("/")
def health_check(): 
    return {"status": "fast_regex_active", "version": "v11.0-Perfect-Name-Display"}

import os
import re
import json
import random
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="飯糰小幫手 ｜ 揪團結算 SaaS 終極完全體")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 🔥 請填入你在 LINE Developers 後台看到的 LIFF ID
MY_LIFF_ID = "YOUR_LIFF_ID_HERE" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Firebase Firestore 初始化驗證
if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] 成功建立 Firestore 安全連線通道！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域狀態機與強型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]
PENDING_CONFIRMATIONS = {}

# 🚀 【核心狀態機快取】紀錄目前各個群組的運作模式
GROUP_STATES = {}

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項、要把錢還給別人的那個人名字。如果使用者說的是『我』，請填寫『發話者』")
    receiver_name: str = Field(description="收到款項、拿回錢的那個人名字。如果使用者說的是『我』，請填寫『發話者』")
    amount: int = Field(default=0, description="核銷、還錢的具體金額")

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐或購買這個東西的人的名字，如果自稱我，請寫『發話者』")
    item_name: str = Field(description="購買的品項名稱")
    price: int = Field(description="該品項的單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive", "settlement", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流。order_item:訂單模式中紀錄品項金額, order_end:訂單結束, order_start:開啟訂單, settle_start:進入結算, settle_pay:登記收付款項, settle_query:查詢已付或未付明細對帳"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list, description="點單模式中拆解出來的品項與金額清單")
    target_payer: Optional[str] = Field(default="", description="訂單結束時，指定最後買單付款的人名字")
    target_order_id: Optional[str] = Field(default="", description="結算模式中，使用者輸入的日期與編號代碼，例如 0620 #8821")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def get_line_user_profile(user_id: str) -> str:
    """透過 LINE 官方 API 逆查成員真實暱稱，避免資料庫出現未知記帳"""
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200: 
            return res.json().get("displayName", "未知成員")
    except Exception: 
        pass
    return "群組夥伴"

def is_pure_category_and_amount(user_text: str) -> Optional[List[SingleRecord]]:
    """極速判斷：若純打「便當 120」，免經大腦直接落庫"""
    text_clean = user_text.strip()
    if len(text_clean) > 10 or any(k in text_clean for k in ["今天", "昨天", "了", "哈哈", "嗨", "核銷", "還", "給"]): 
        return None
    numbers_find = list(re.finditer(r'\d+', text_clean))
    if len(numbers_find) != 1: 
        return None
    try:
        match = numbers_find[0]
        amount = int(match.group())
        prev_text = text_clean[:match.start()].strip()
        next_text = text_clean[match.end():].strip()
        clean_prev = re.sub(r'[^一-龥a-zA-Z]', '', prev_text)
        clean_next = re.sub(r'[^一-龥a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
        item = clean_prev if clean_prev else (clean_next if clean_next else "日常支出")
        
        category = "生活雜費"
        official_categories = ["餐飲食品", "交通運輸", "娛樂休閒", "生活雜費", "服飾美容", "醫療保健", "薪資收入", "投資理財", "其他收入"]
        for cat in official_categories:
            if cat[:2] in item or item in cat: 
                category = cat
                break
        r_type = "income" if any(k in item for k in ["薪水", "收入", "賺"]) else "expense"
        return [SingleRecord(record_type=r_type, amount=amount, item=item, category=category, note="⚡ 本地極速")]
    except Exception: 
        return None

def analyze_with_gemini_sync(user_text: str, current_mode: str = "normal") -> SuperRouter:
    """【大腦】根據目前的群組狀態模式，動態調整 Prompt 進行智慧分流與防噪過濾"""
    prompt = f"""
    你是一個具備頂級控場能力的財務記帳助理「飯糰小幫手」。目前群組處於【{current_mode}】模式。
    請透視分析使用者的語意輸入：『{user_text}』進行強型別分流。
    
    【核心分流規則】：
    1. 如果提及「開啟訂單」、「團購開始」、「開團」，intent 務必歸為 order_start。
    2. 如果提及「訂單結束」、「結單」、「截止」，intent 務必歸為 order_end。
    3. 如果提及「訂單結算」、「結算訂單」，intent 務必歸為 settle_start。
    """
    
    if current_mode == "order":
        prompt += """
        4. 當前為【訂單模式】：群組成員正在熱烈點單。只要有提到任何品項和金額（例如：牛肉麵 150、大杯珍奶 60、小明 雞排 95），
           請將 intent 歸類為 order_item，並精準拆解到 order_items 陣列中（若自稱我或沒寫名字，買家名字請填寫『發話者』）。
           如果對話內容完全沒有提及任何金額與物品（例如純日常聊天：『對啊這家好吃』、『哈哈笑死』），請直接將 intent 歸類為 chat，且 ai_reply 留空，我們會予以過濾無視。
        """
    elif current_mode == "settle":
        prompt += """
        5. 當前為【結算模式】：成員正在主動交錢還款。
           - 如果使用者是在問「誰沒給錢」、「誰未給錢」、「未付明細」、「對帳」、「誰還沒付」，intent 務必歸為 settle_query。
           - 如果使用者輸入符合「我給了 @阿誠 150」、「小明 還 墊款人 95」，intent 務必歸為 settle_pay，並拆解到 settlement 結構中（若自稱我，名字請填寫『發話者』）。
           - 如果是其他任何無關的閒聊雜訊，請將 intent 歸類為 chat，且 ai_reply 留空，系統會主動阻斷。
        """
    else:
        prompt += """
        6. 當前為【常態模式】：支援普通記帳(record)、普通還錢核銷(settlement)與當月數據分析(analyze)。
           如果在常規核銷中提及還我錢、給我錢等「我」，名字也請填寫『發話者』。
        """

    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
    )
    if response.parsed: 
        return response.parsed
    return SuperRouter(**json.loads(response.text))

def save_records_to_db_v2(target_id: str, is_group: bool, creator_id: str, records: List[SingleRecord]) -> bool:
    if db is None or not records: 
        return False
    try:
        creator_name = get_line_user_profile(creator_id)
        if is_group:
            base_ref = db.collection("groups").document(target_id)
            if not base_ref.get().exists: 
                base_ref.set({"group_id": target_id, "created_at": datetime.utcnow()})
        else:
            base_ref = db.collection("users").document(target_id)
            if not base_ref.get().exists: 
                base_ref.set({"line_user_id": target_id, "display_name": creator_name, "created_at": datetime.utcnow()})
        
        batch = db.batch()
        for rec in records:
            if rec.amount <= 0: 
                continue
            doc_ref = base_ref.collection("expenses").document()
            batch.set(doc_ref, {
                "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category, "note": rec.note,
                "timestamp": datetime.utcnow(), "created_by_uid": creator_id, "created_by_name": creator_name
            })
        batch.commit()
        return True
    except Exception: 
        return False

def get_monthly_quick_summary_v2(target_id: str, is_group: bool) -> str:
    if db is None: 
        return "📴 資料庫系統維護中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        if is_group:
            exp_query = db.collection("groups").document(target_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
            settle_query = db.collection("groups").document(target_id).collection("settlements").where("timestamp", ">=", start_of_month).stream()
            expense_total = sum(doc.to_dict().get("amount", 0) for doc in exp_query if doc.to_dict().get("type") == "expense")
            settle_count = len(list(settle_query))
            return f"👥 【本月群組帳務速報】\n📉 當月公帳總開銷：${expense_total:,}\n🤝 已登錄核銷筆數：{settle_count} 筆"
        else:
            query = db.collection("users").document(target_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
            income_total = 0; expense_total = 0
            for doc in query:
                data = doc.to_dict(); amt = data.get("amount", 0)
                if data.get("type", "expense") == "income": 
                    income_total += amt
                else: 
                    expense_total += amt
            return f"📊 【本月個人財務速報】\n📈 當月總收入：${income_total:,}\n📉 當月總支出：${expense_total:,}\n💰 錢包淨結餘：${(income_total - expense_total):,}"
    except Exception: 
        return "⚠️ 讀取當月資料庫速報時發生延遲"

# ==========================================
# 🌐 4. Webhook 入口與狀態機調度主線
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: 
        raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    if body_str and '"text":"請教導我該如何使用？"' in body_str: 
        return Response(content="OK", status_code=200)
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: 
        handler.handle(body_str, signature)
    except InvalidSignatureError: 
        print("❌ LINE Webhook 簽章密鑰驗證失敗！", flush=True)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    target_id = event.source.group_id if is_group else creator_id

    # 📥 讀取或初始化群組當前模式狀態
    if target_id not in GROUP_STATES:
        GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
    
    current_state = GROUP_STATES[target_id]
    current_mode = current_state["mode"]

    # 🚀 【核心群組防噪與節流過濾機制】
    is_mentioned = False
    if is_group:
        mention = getattr(event.message, "mention", None)
        if mention and mention.mentionees: 
            is_mentioned = True
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開啟訂單", "訂單結束", "訂單結算"]): 
            is_mentioned = True
        
        # 💡 防火牆 A：若在「訂單模式」，且訊息內有數字，強制放行給 AI 漏斗判定是否為點餐
        if current_mode == "order" and re.search(r'\d+', user_text): 
            is_mentioned = True
        # 💡 防火牆 B：若在「結算模式」，且提到對帳關鍵字或還款關鍵字，放行勾稽
        if current_mode == "settle" and any(k in user_text for k in ["給", "還", "付", "誰沒給", "誰未給", "未付"]): 
            is_mentioned = True
        
        if not is_mentioned and creator_id not in PENDING_CONFIRMATIONS:
            return # 沒有被喚醒，直接安靜無視，全面阻斷並節省 Token 成本

        user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()

    reply_str = ""

    # 🛑 攔截機制：全域敏感詞防禦
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            reply_str = "🤖 飯糰小幫手為純財務系統，無法回應與處理敏感話題喔！"
            try:
                with ApiClient(line_config) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)]))
                return
            except Exception: 
                return

    # 🔄 狀態機攔截：確認常規記帳狀態機
    if creator_id in PENDING_CONFIRMATIONS and current_mode == "normal":
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "yes", "OK"]:
            saved_records = PENDING_CONFIRMATIONS.pop(creator_id)
            db_success = save_records_to_db_v2(target_id, is_group, creator_id, saved_records)
            reply_str = "👌 收到！已成功幫您記錄這筆花費至雲端！" if db_success else "⚠️ 雲端備份稍微延遲，請稍後檢查。"
        else:
            PENDING_CONFIRMATIONS.pop(creator_id, None)
            reply_str = "❌ 已取消該筆紀錄，大腦將不記入帳本。"
    else:
        # 🧠 進入核心運算大腦
        try:
            result = analyze_with_gemini_sync(user_text, current_mode)
            creator_name = get_line_user_profile(creator_id)

            # ----------------------------------------------------
            # 模式 1：開啟訂單 (order_start)
            # ----------------------------------------------------
            if result.intent == "order_start":
                GROUP_STATES[target_id] = {"mode": "order", "order_items": []}
                reply_str = "🚀 【飯團團購模式・正式啟動】\n🤖 小幫手已進入高效點單過濾狀態！\n👉 請大家直接輸入「品項 + 金額」（例如：牛肉麵 150），我會自動歸檔。非點單的閒聊文字我會主動無視喔！"

            # ----------------------------------------------------
            # 模式 2：訂單模式下的品項自動蒐集 (order_item)
            # ----------------------------------------------------
            elif result.intent == "order_item" and current_mode == "order":
                if result.order_items:
                    for item in result.order_items:
                        buyer = item.buyer_name.strip()
                        # 🎯 智慧身份補位：若自稱我或空白，自動帶入發話者真实暱稱
                        if buyer == "發話者" or not buyer: 
                            buyer = creator_name
                        
                        GROUP_STATES[target_id]["order_items"].append({
                            "buyer": buyer, "item": item.item_name, "price": item.price
                        })
                    
                    lines = [f"・{i['buyer']} 已點：{i['item']} ${i['price']}" for i in result.order_items]
                    reply_str = "📝 【訂單明細已掛載】\n" + "\n".join(lines)
                else:
                    return # 確定為雜訊，直接阻斷不回覆

            # ----------------------------------------------------
            # 模式 3：訂單結束、生成單號封存 (order_end)
            # ----------------------------------------------------
            elif result.intent == "order_end" and current_mode == "order":
                order_items = current_state["order_items"]
                if not order_items:
                    GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
                    reply_str = "🛑 該團購因無人點單，已自動關閉並退回常態模式。"
                else:
                    master_payer = result.target_payer.strip() if result.target_payer else creator_name
                    if master_payer == "發話者": 
                        master_payer = creator_name
                    
                    # 自動生成隨機 4 位數單號與日期
                    date_str = datetime.now().strftime("%m%d")
                    code_str = str(random.randint(1000, 9999))
                    order_doc_id = f"{datetime.now().strftime('%Y%m%d')}_{code_str}"
                    total_amt = sum(i["price"] for i in order_items)
                    
                    if db:
                        db.collection("groups").document(target_id).collection("orders").document(order_doc_id).set({
                            "order_date": datetime.now().strftime("%Y-%m-%d"),
                            "order_code": code_str,
                            "total_amount": total_amt,
                            "master_payer_name": master_payer,
                            "items": order_items,
                            "status": "pending",
                            "timestamp": datetime.utcnow()
                        })
                    
                    GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
                    reply_str = f"🏁 【團購訂單安全截止】\n📅 訂單日期：{date_str}\n🔢 結算編號：#{code_str}\n💰 總金額：${total_amt:,} 元\n💳 墊款買單人：{master_payer}\n\n🤖 數據已安全封存！後續要收錢時，請輸入「訂單結算 {date_str} #{code_str}」即可調閱控制台！"

            # ----------------------------------------------------
            # 模式 4：啟動訂單催款結算控制台 (settle_start)
            # ----------------------------------------------------
            elif result.intent == "settle_start":
                match_code = re.search(r'(\d{4})\s*#?(\d{4})', user_text)
                if not match_code:
                    reply_str = "⚠️ 請輸入正確的結算格式！例如：「訂單結算 0620 #8821」"
                else:
                    req_code = match_code.group(2)
                    order_found = None
                    if db:
                        orders = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", req_code).stream()
                        for doc in orders:
                            order_found = doc.to_dict()
                            break
                    
                    if not order_found:
                        reply_str = f"❌ 找不到編號為 #{req_code} 的訂單，請確認編號是否輸入正確！"
                    else:
                        # 🔒 鎖定群組切入「結算模式防火牆」
                        GROUP_STATES[target_id] = {
                            "mode": "settle",
                            "active_order_code": req_code,
                            "master_payer": order_found["master_payer_name"]
                        }
                        items_list = order_found["items"]
                        lines = [f"・{i['buyer']} 点了 【{i['item']}】 🪙 應付：${i['price']}" for i in items_list]
                        
                        reply_str = f"🔔 【飯糰訂單催款控制台 ｜ 結算模式】\n🔢 訂單單號：#{req_code}\n💳 墊款債權人：{order_found['master_payer_name']}\n\n📋 應收明細：\n" + "\n".join(lines) + f"\n\n🛑 【結算防火牆已啟動】：現在群組僅受理還款對帳，若要查看誰沒給錢請輸入「誰未給錢」，核銷完畢請輸入「結算結束」。"

            # ----------------------------------------------------
            # 模式 5：結算模式下的勾稽付款 (settle_pay)
            # ----------------------------------------------------
            elif result.intent == "settle_pay" and current_mode == "settle":
                if result.settlement:
                    s = result.settlement
                    p_name = s.payer_name.strip()
                    r_name = s.receiver_name.strip()
                    
                    # 🎯 雙向身份自動補位
                    if p_name == "發話者" or not p_name: 
                        p_name = creator_name
                    if r_name == "發話者" or not r_name: 
                        r_name = current_state["master_payer"]
                    
                    if p_name == r_name:
                        return # 防止自言自語數據污染
                    
                    if db:
                        db.collection("groups").document(target_id).collection("settlements").document().set({
                            "payer_name": p_name, "receiver_name": r_name, "amount": s.amount,
                            "order_code_ref": current_state["active_order_code"], "timestamp": datetime.utcnow()
                        })
                    
                    reply_str = f"🤝 【訂單核銷對帳成功】\n✅ {p_name} 成功付款給 {r_name} ${s.amount:,} 元！\n\n💡 提示：可隨時輸入「誰未給錢」查核最新對帳報表。"
                else:
                    return # 雜訊直接阻斷

            # ----------------------------------------------------
            # 模式 6：【全新核心】已付款與未付款明細實時對帳查詢 (settle_query)
            # ----------------------------------------------------
            elif result.intent == "settle_query" and current_mode == "settle":
                active_code = current_state.get("active_order_code")
                if not db or not active_code:
                    reply_str = "⚠️ 系統目前無法讀取當前活躍的訂單單號。"
                else:
                    # A. 撈取原始點單應付名單
                    order_doc = None
                    orders = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", active_code).stream()
                    for doc in orders:
                        order_doc = doc.to_dict()
                        break
                    
                    if not order_doc:
                        reply_str = "❌ 找不到當前活躍訂單的原始封存資料。"
                    else:
                        # 彙整每個人應該付的總金額
                        expected_payments = {}
                        for item in order_doc.get("items", []):
                            b_name = item["buyer"]
                            expected_payments[b_name] = expected_payments.get(b_name, 0) + item["price"]
                        
                        # B. 撈取此單目前已經登記的實付金額
                        settles = db.collection("groups").document(target_id).collection("settlements").where("order_code_ref", "==", active_code).stream()
                        actual_paid = {}
                        for doc in settles:
                            s_data = doc.to_dict()
                            p_name = s_data.get("payer_name")
                            actual_paid[p_name] = actual_paid.get(p_name, 0) + s_data.get("amount", 0)
                        
                        # C. 實時雙向聯集比對
                        paid_lines = []
                        unpaid_lines = []
                        for person, r_amount in expected_payments.items():
                            has_paid = actual_paid.get(person, 0)
                            if has_paid >= r_amount:
                                paid_lines.append(f" 🟢 {person} 已全額付清 (${r_amount} 元)")
                            elif has_paid > 0:
                                remains = r_amount - has_paid
                                unpaid_lines.append(f" 🟡 {person} 已局部付 ${has_paid}，⚠️ 還差 (${remains} 元)")
                            else:
                                unpaid_lines.append(f" 🔴 {person} 尚未付款 🪙 應付：${r_amount} 元")
                        
                        # D. 建構可視化對帳表
                        reply_str = f"📊 【訂單 #{active_code} 當前對帳進度報表】\n"
                        reply_str += f"💳 墊款債權人：{order_doc['master_payer_name']}\n"
                        reply_str += f"💰 訂單總金額：${order_doc['total_amount']:,} 元\n"
                        reply_str += "-------------------------\n"
                        if unpaid_lines:
                            reply_str += "❌ 【尚未付清名單】:\n" + "\n".join(unpaid_lines) + "\n"
                        else:
                            reply_str += "🎉 【太讚了！全員皆已付清！】\n"
                        if paid_lines:
                            reply_str += "\n✅ 【已付清名單】:\n" + "\n".join(paid_lines)

            # ----------------------------------------------------
            # 模式 7：結束結算模式，回歸常態
            # ----------------------------------------------------
            elif "結算結束" in user_text and current_mode == "settle":
                GROUP_STATES[target_id] = {"mode": "normal", "order_items": []}
                reply_str = "🔓 【結算控制台關閉】已成功解鎖防火牆，群組已恢復常態對話與記帳模式！"

            # ----------------------------------------------------
            # 模式 8：常態模式下的記帳、常規核銷與分析 (normal)
            # ----------------------------------------------------
            elif current_mode == "normal":
                if result.intent == "record" and result.records:
                    db_success = save_records_to_db_v2(target_id, True, creator_id, result.records)
                    if db_success:
                        lines = [f"➖ 支出 ${r.amount} ({r.item})" for r in result.records]
                        reply_str = f"👥 【群組公帳】{creator_name} 記帳成功！\n" + "\n".join(lines)
                elif result.intent == "settlement" and result.settlement:
                    s = result.settlement
                    p = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name
                    r = creator_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name
                    
                    if db:
                        db.collection("groups").document(target_id).collection("settlements").document().set({
                            "payer_name": p, "receiver_name": r, "amount": s.amount, "timestamp": datetime.utcnow()
                        })
                    reply_str = f"🤝 【群組常規核銷成功】\n💸 付款人：{p}\n📥 收款人：{r}\n💰 金額：${s.amount:,} 元"
                elif result.intent == "analyze":
                    summary_text = get_monthly_quick_summary_v2(target_id, True)
                    dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}?groupId={target_id}"
                    reply_str = f"{summary_text}\n\n🌐 雲端可視化大後台：\n{dashboard_url}"
                else:
                    if result.ai_reply: 
                        reply_str = result.ai_reply

        except Exception as e:
            print(f"🧠 大腦運算異常: {e}")
            if current_mode in ["order", "settle"]: 
                return # 揪團與結算控制期間若出錯，直接靜默阻斷，確保極致體驗

    # 強型別防禦：防空訊息
    if not reply_str or reply_str.strip() == "": 
        return  

    # 執行 LINE 推播發送
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)]))
    except Exception as e: 
        print(f"❌ LINE 訊息發送失敗: {e}", flush=True)

@app.get("/")
def health_check(): 
    return {"status": "healthy", "version": "v4.5-SaaS-OrderSettlement-Final"}

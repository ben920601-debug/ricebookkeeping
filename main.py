import os
import json
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,  # 🚀 改用 Push Message (主動推播) 徹底解決 LINE 5秒逾時斷線
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

app = FastAPI()

# ==========================================
# ⚙️ 基礎環境變數與客戶端初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 🚀 初始化 Gemini 2.5 Flash 付費版大腦 (無設定超時，背景好整以暇慢慢算)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 🔥 Firebase Firestore 實體檔案安全性初始化 (讀取 Render Secret File)
cred_path = "firebase-adminsdk.json"
if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"🔥 [DATABASE LOG] 成功讀取 {cred_path}，Firestore 初始化成功！")
    except Exception as e:
        db = None
        print(f"❌ [DATABASE LOG] 找到檔案但初始化崩潰: {e}")
else:
    db = None
    print(f"❌ [DATABASE LOG] 嚴重錯誤：在雲端根目錄找不到 {cred_path} 檔案！")

# ==========================================
# 📊 Pydantic 資料結構定義 (強型別約束)
# ==========================================
class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense", description="expense: 支出, income: 收入")
    amount: int = Field(default=0, description="金額")
    item: str = Field(default="", description="項目名稱")
    category: str = Field(default="生活雜費", description="限用: 餐飲食品、交通運輸、娛樂休閒、生活雜費、服飾美容、醫療保健、薪資收入、投資理財、其他收入")
    note: str = Field(default="", description="備註")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive"] = Field(description="意圖分流")
    records: Optional[List[SingleRecord]] = Field(default_factory=list, description="收支明細陣列")
    ai_reply: Optional[str] = Field(default="", description="回應文字")

# ==========================================
# ⚡ 智慧分流攔截器 (Token 節流核心)
# ==========================================

def is_pure_category_and_amount(user_text: str) -> Optional[List[SingleRecord]]:
    """🚀 前線攔截過濾器：判斷是否『僅有類別/項目 + 金額』
    如果是，直接在本地用 Python 封裝 records 並回傳，拒絕浪費 Gemini Token！
    """
    text_clean = user_text.strip()
    
    # 策略 1：如果字數太長（超過 10 個字），通常代表有聊天口吻，放行給 Gemini
    if len(text_clean) > 10:
        return None
        
    # 策略 2：如果包含常見的日常聊天、語氣助詞，放行給 Gemini 做高情商對話
    chat_keywords = ["今天", "昨天", "明天", "跟", "去", "吃", "了", "哈哈", "嗨", "你好", "幫我", "我想"]
    if any(k in text_clean for k in chat_keywords):
        return None

    # 開始用 Python 正則表達式拆解數字
    numbers_find = list(re.finditer(r'\d+', text_clean))
    
    # 必須剛好只包含一組數字（一筆金額），多了或沒數字都交給 Gemini 處理
    if len(numbers_find) != 1:
        return None
        
    try:
        match = numbers_find[0]
        amount = int(match.group())
        start_pos = match.start()
        end_pos = match.end()
        
        # 拆出數字以外的文字，當作項目與分類
        prev_text = text_clean[:start_pos].strip()
        next_text = text_clean[end_pos:].strip()
        
        # 清理掉「元」、「塊」等常規單位符號
        clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text)
        clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
        
        # 決定項目名稱
        item = clean_prev if clean_prev else (clean_next if clean_next else "日常支出")
        
        # 智慧對齊官方 9 大分類
        category = "生活雜費"
        official_categories = ["餐飲食品", "交通運輸", "娛樂休閒", "生活雜費", "服飾美容", "醫療保健", "薪資收入", "投資理財", "其他收入"]
        
        # 如果使用者輸入的關鍵字有命中官方分類，自動歸類 (例：輸入 "餐飲" -> "餐飲食品")
        for cat in official_categories:
            if cat[:2] in item or item in cat:
                category = cat
                break
                
        # 判斷是收入還是支出
        r_type = "income" if any(k in item for k in ["薪水", "收入", "中獎", "賺", "薪資"]) else "expense"
        if r_type == "income" and category == "生活雜費":
            category = "薪資收入"

        # 100% 信心度：Python 本地成功攔截並封裝！
        return [SingleRecord(
            record_type=r_type, amount=amount, item=item, category=category, note="⚡ Python 本地極速記帳"
        )]
    except Exception:
        return None

# ==========================================
# 🤖 核心 AI 大腦與保底邏輯
# ==========================================

def analyze_with_gemini_sync(user_text: str) -> SuperRouter:
    """【大腦】Gemini 2.5 Flash 純同步調用，在背景執行緒中運行安全不卡死"""
    prompt = f"""
    你是一個極簡現代風格的個人財務助理「飯糰小幫手」。請分析使用者的輸入：『{user_text}』
    
    請遵守以下規則：
    1. 【主動記帳 (record)】：無論是支出還是收入，精準判斷並拆解存入 records 陣列。
    2. 【對話中提及收支 (chat_with_record)】：聊天時提到賺錢或花錢。在 ai_reply 用「極其精簡、現代溫暖」的一句話詢問是否要記帳。
    3. 【純聊天 (chat)】：不含收支的日常問候。在 ai_reply 給出高情商且極簡的回應。此時 records 請務必給空陣列 []。
    4. 【回應風格】：說話俐落，不長篇大論。
    """
    
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', 
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SuperRouter,
            temperature=0.3  # 較低的溫度，確保強型別 JSON 輸出精準快速
        ),
    )
    
    if response.parsed:
        return response.parsed
    return SuperRouter(**json.loads(response.text))


def analyze_with_python_fallback(user_text: str) -> SuperRouter:
    """【最終防線】當網路極度異常、AI 全面斷線時，Python 規則自動代打"""
    user_text_lower = user_text.lower().strip()
    if any(k in user_text_lower for k in ["查", "報表", "分析", "統計", "花多少", "結餘", "速報"]):
        return SuperRouter(intent="analyze")
        
    numbers_find = re.finditer(r'\d+', user_text)
    records = []
    try:
        for match in numbers_find:
            amount = int(match.group())
            start_pos = match.start()
            end_pos = match.end()
            prev_text = user_text[max(0, start_pos-5):start_pos].strip()
            next_text = user_text[end_pos:min(len(user_text), end_pos+5)].strip()
            clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text).replace("花了", "").replace("吃了", "")
            clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
            item = clean_prev if (clean_prev and len(clean_prev) >= 2) else (clean_next if clean_next else "日常收支")
            r_type = "income" if any(k in user_text for k in ["薪水", "收入", "中獎", "賺", "薪資"]) else "expense"
            records.append(SingleRecord(record_type=r_type, amount=amount, item=item, category="生活雜費", note="⚠️ 備用大腦解析"))
            
        if records:
            return SuperRouter(intent="chat_with_record", records=records, ai_reply="⚠️ 系統繁忙中，已啟動安全確認機制。")
    except Exception: pass
    return SuperRouter(intent="chat", ai_reply="👌")

# ==========================================
# 💾 資料庫讀寫與速報邏輯
# ==========================================
def get_line_user_profile(user_id: str) -> str:
    try:
        with ApiClient(line_config) as api_client:
            return MessagingApi(api_client).get_profile(user_id).display_name
    except Exception: return "飯糰友"

def save_records_to_db(user_id: str, records: List[SingleRecord]):
    if db is None or not records: return False
    try:
        user_ref = db.collection("users").document(user_id)
        if not user_ref.get().exists:
            user_ref.set({"line_user_id": user_id, "display_name": get_line_user_profile(user_id), "created_at": datetime.utcnow()})
        batch = db.batch()
        for rec in records:
            if rec.amount <= 0: continue
            batch.set(user_ref.collection("expenses").document(), {
                "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category, "note": rec.note, "timestamp": datetime.utcnow()
            })
        batch.commit()
        return True
    except Exception: return False

def get_monthly_quick_summary(user_id: str) -> str:
    if db is None: return "📴 資料庫維換中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        query = db.collection("users").document(user_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
        income_total = 0; expense_total = 0
        for doc in query:
            data = doc.to_dict(); amt = data.get("amount", 0)
            if data.get("type", "expense") == "income": income_total += amt
            else: expense_total += amt
        return f"📊 本月極簡速報\n📈 總收入：${income_total}\n📉 總支出：${expense_total}\n💰 淨結餘：${income_total - expense_total}\n\n🌐 詳細明細請至 Web 後台查看。"
    except Exception: return "⚠️ 查詢速報暫時失敗"

# ==========================================
# 🌐 Webhook 入口與多執行緒異步調度
# ==========================================
PENDING_CONFIRMATIONS = {}

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    """🚀 核心機制：0.1 秒極速秒回 LINE 200 OK，把重度 AI 任務打包丟到背景，徹底防禦逾時！"""
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # 丟給 FastAPI 背景執行緒，立刻中斷與 LINE 的逾時倒數
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return "OK"


def handle_line_events_safe(body_str: str, signature: str):
    try: handler.handle(body_str, signature)
    except InvalidSignatureError: print("❌ LINE 簽章驗證失敗")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """此處運作於背景安全線程，自由調配 Python 攔截與 Gemini 運算，無懼逾時限制"""
    user_text = event.message.text.strip()
    user_id = event.source.user_id 
    reply_str = ""
    
    # 1. 狀態機快捷確認優先處理
    if user_id in PENDING_CONFIRMATIONS:
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "幫我記", "yes", "correct"]:
            saved_records = PENDING_CONFIRMATIONS.pop(user_id)
            db_success = save_records_to_db(user_id, saved_records)
            reply_str = "👌 已幫您安全記入帳本！" if db_success else "⚠️ 寫入失敗。"
        else:
            PENDING_CONFIRMATIONS.pop(user_id, None) 
            reply_str = "❌ 抱歉抓錯了！已取消該筆紀錄，請重新輸入。✍️"
            
    else:
        # 🚀 2. 智慧分流攔截檢測 (只包含類別與金額)
        local_records = is_pure_category_and_amount(user_text)
        
        if local_records:
            # 🎯 命中純記帳短語：Python 直接入庫直出，不消耗任何 Gemini Token
            print("⚡ [LINE LOG] 偵測到純記帳短語，由 Python 本地直接直出，省下 Token！")
            db_success = save_records_to_db(user_id, local_records)
            if db_success:
                lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item} ➡️ {r.category})" for r in local_records]
                reply_str = "✅ 記帳成功！\n" + "\n".join(lines)
            else:
                reply_str = "⚠️ 備份延遲。"
                
        else:
            # 🤖 未命中純記帳格式：代表是複雜對話或查詢，調度 Gemini 大腦
            print("🧠 [LINE LOG] 偵測到複雜對話或報表查詢，調度 Gemini 大腦...")
            try:
                result = analyze_with_gemini_sync(user_text)
                print("🤖 [LINE LOG] Gemini 運算成功！")
                
                if result.intent == "record" and result.records:
                    db_success = save_records_to_db(user_id, result.records)
                    if db_success:
                        lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in result.records]
                        reply_str = "✅ 記帳 success！\n" + "\n".join(lines)
                    else: reply_str = "⚠️ 備份延遲。"
                elif result.intent == "chat_with_record" and result.records:
                    PENDING_CONFIRMATIONS[user_id] = result.records
                    reply_str = f"{result.ai_reply}\n\n🔍 偵測到以下可能的花費：\n"
                    for rec in result.records:
                        reply_str += f"・[{'收入' if rec.record_type == 'income' else '支出'}] ${rec.amount} 元 的 {rec.item}\n"
                    reply_str += "\n👉 正確請回覆「好」，若錯誤請回覆任意文字來重新輸入。"
                elif result.intent == "analyze": 
                    reply_str = get_monthly_quick_summary(user_id)
                elif result.intent == "chat" or result.intent == "sensitive": 
                    reply_str = result.ai_reply
                else: 
                    reply_str = "👌"
                    
            except Exception as gemini_err:
                print(f"❌ Gemini 運行異常 ({gemini_err}) ➡️ 降級至 Python 保底機制")
                fallback_result = analyze_with_python_fallback(user_text)
                if fallback_result.intent == "analyze":
                    reply_str = get_monthly_quick_summary(user_id)
                elif fallback_result.intent == "chat_with_record":
                    PENDING_CONFIRMATIONS[user_id] = fallback_result.records
                    reply_str = f"{fallback_result.ai_reply}\n👉 請回覆「好」確認記帳。"
                else:
                    reply_str = fallback_result.ai_reply

    # 🚀 3. 主動推播 (Push Message) 回傳使用者手機
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=reply_str)])
            )
    except Exception as e: 
        print(f"❌ 主動推播失敗: {e}")       elif result.intent == "analyze": reply_str = get_monthly_quick_summary(user_id)
        elif result.intent == "chat" or result.intent == "sensitive": reply_str = result.ai_reply
        else: reply_str = "👌"

    # 🚀 關鍵優化：改用 Push Message 主動推播回使用者的手機上
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=reply_str)])
            )
    except Exception as e: 
        print(f"❌ 主動推播失敗: {e}")

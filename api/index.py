import os
import json
import requests
import base64
from fastapi import FastAPI, Request, Response, BackgroundTasks
import firebase_admin
from firebase_admin import credentials, firestore
from openai import OpenAI  
from dotenv import load_dotenv

load_dotenv()

app = FastAPI() 

# ---- 🛠️ SAFE FIREBASE INITIALIZATION ----
db = None

if not firebase_admin._apps:
    try:
        fb_creds_raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        if fb_creds_raw:
            fb_creds = json.loads(fb_creds_raw)
            if "private_key" in fb_creds:
                fb_creds["private_key"] = fb_creds["private_key"].replace('\\n', '\n')
                
            cred = credentials.Certificate(fb_creds)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("Firebase successfully connected!")
    except Exception as e:
        print(f"Firebase Initialization Error: {e}")
else:
    try:
        db = firestore.client()
    except Exception as e:
        print(f"Firebase Client Fetch Error: {e}")

# ---- 🤖 GROQ AI CLIENT SETUP ----
openai_key = os.environ.get("OPENAI_API_KEY") 
ai_client = None
if openai_key:
    ai_client = OpenAI(
        api_key=openai_key,
        base_url="https://api.groq.com/openai/v1"
    )
else:
    print("CRITICAL: OPENAI_API_KEY (Groq Key) env variable is missing!")

FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN")


# ---- 📬 MESSENGER HELPER FUNCTIONS ----

def send_fb_message(recipient_id, text):
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    headers = {"Content-Type": "application/json"}
    requests.post(url, json=payload, headers=headers)


def send_product_carousel(recipient_id, products):
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    elements = []
    for p in products:
        elements.append({
            "title": p.get('name', 'E-commerce Product'),
            "image_url": p.get('image_url', ''),
            "subtitle": f"Price: {p.get('price', 'N/A')}\n{p.get('description', '')}",
            "buttons": [{"type": "web_url", "url": "https://yourwebsite.com/checkout", "title": "Buy Now"}]
        })
        
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements[:10]
                }
            }
        }
    }
    headers = {"Content-Type": "application/json"}
    requests.post(url, json=payload, headers=headers)


def get_all_products():
    global db
    if db is None:
        return []
    try:
        products_ref = db.collection('products').stream()
        product_list = []
        for p in products_ref:
            p_data = p.to_dict()
            p_data['id'] = p.id
            product_list.append(p_data)
        return product_list
    except Exception as e:
        print(f"Firestore Fetch Error: {e}")
        return []

# ---- 🖼️ IMAGE TO BASE64 HELPER FUNCTION ----
def get_image_base64_from_url(url):
    """ফেসবুকের ইউআরএল থেকে ইমেজ ডাউনলোড করে Base64 স্ট্রিংয়ে রূপান্তর করে"""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            # কন্টেন্ট টাইপ চেক করা (যেমন: image/jpeg, image/png)
            content_type = response.headers.get('Content-Type', 'image/jpeg')
            encoded_string = base64.b64encode(response.content).decode('utf-8')
            return f"data:{content_type};base64,{encoded_string}"
    except Exception as e:
        print(f"Error downloading or encoding image: {e}")
    return None


# ---- ⚡ BACKGROUND PROCESSING ENGINE ----
def process_webhook_event(messaging_event):
    sender_id = messaging_event["sender"]["id"]
    
    # --- 📸 IMAGE HANDLE (GROQ VISION WITH BASE64 FIX) ---
    if "message" in messaging_event and "attachments" in messaging_event["message"]:
        for attachment in messaging_event["message"]["attachments"]:
            if attachment["type"] == "image":
                image_url = attachment["payload"]["url"]
                send_fb_message(sender_id, "আপনার দেওয়া ছবিটি স্ক্যান করা হচ্ছে, একটু অপেক্ষা করুন...")
                
                all_products = get_all_products()
                if not all_products:
                    send_fb_message(sender_id, "দুঃখিত, আমাদের ডাটাবেজ কানেকশন এখন অফলাইন।")
                    return
                
                # ফেসবুক ইউআরএল এর বদলে ইমেজ ডেটা Base64 এ কনভার্ট করা হচ্ছে
                base64_image = get_image_base64_from_url(image_url)
                if not base64_image:
                    send_fb_message(sender_id, "দুঃখিত, ছবিটি ডাউনলোড করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
                    return
                
                try:
                    system_prompt = (
                        "You are a strict database matcher. Compare the user's image with this product list: "
                        f"{json.dumps(all_products)}. Identify if the image matches any product. "
                        "CRITICAL: Your response must be EXACTLY the 'id' of the matched product, or the word 'None'. "
                        "Do not include any greeting, punctuation, explanation, or markdown formatting."
                    )
                    
                    response = ai_client.chat.completions.create(
                        model="llama-3.2-11b-vision-instant",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "What is the ID of this product from the database?"},
                                    {
                                        "type": "image_url", 
                                        "image_url": {"url": base64_image} # এখানে Base64 ডেটা পাস করা হয়েছে
                                    },
                                ],
                            }
                        ],
                        max_tokens=10,
                        temperature=0.0
                    )
                    
                    matched_id = response.choices[0].message.content.strip()
                    print(f"Groq Vision Matched ID: {matched_id}") # ডিবাগিং এর জন্য প্রিন্ট
                    
                    if matched_id and matched_id != "None" and db is not None:
                        product_doc = db.collection('products').document(matched_id).get().to_dict()
                        if product_doc:
                            send_product_carousel(sender_id, [product_doc])
                        else:
                            send_fb_message(sender_id, "প্রোডাক্ট আইডি মিললেও ডাটাবেজে ডিটেইলস পাওয়া যায়নি।")
                    else:
                        send_fb_message(sender_id, "দুঃখিত! এই প্রোডাক্টটি আমাদের ডাটাবেজে খুঁজে পাওয়া যায়নি।")
                        
                except Exception as e:
                    print(f"Groq Vision Error: {e}")
                    send_fb_message(sender_id, "ছবি প্রসেসিংয়ে সমস্যা হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।")

    # --- 💬 TEXT HANDLE (GROQ TEXT) ---
    elif "message" in messaging_event and "text" in messaging_event["message"]:
        user_text = messaging_event["message"]["text"].lower()
        
        if any(word in user_text for word in ["product", "onno", "details", "price", "পণ্য", "দাম"]):
            products = get_all_products()
            if products:
                send_product_carousel(sender_id, products[:10])
            else:
                send_fb_message(sender_id, "স্টক খালি বা ডাটাবেজ অফলাইন।")
        else:
            try:
                system_instruction = (
                    "You are a polite and helpful E-commerce Assistant for an online shop. "
                    "Always reply shortly and friendly in Bengali language (Bangla script). "
                    "CRITICAL: If the customer asks how to buy or order (যেমন: কীভাবে অর্ডার করব, অর্ডার করার নিয়ম কী), "
                    "instruct them politely to visit our website, select their desired product, and complete the order from there. "
                    "If they ask about products or pricing, tell them to type 'product'. "
                    "Keep your responses within 1-2 sentences."
                )
                
                response = ai_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": user_text}
                    ],
                    max_tokens=150
                )
                
                send_fb_message(sender_id, response.choices[0].message.content)
            except Exception as e:
                print(f"Groq Text Error: {e}")
                send_fb_message(sender_id, "আপনাকে কীভাবে সাহায্য করতে পারি? প্রোডাক্ট দেখতে 'product' লিখুন।")


# ---- 🌐 WEBHOOK ROUTING ----

@app.get("/")
def home():
    db_status = "Connected" if db is not None else "Disconnected"
    ai_status = "Ready" if ai_client is not None else "Missing Key"
    return {
        "status": "E-commerce Bot is Live with Free Groq API!",
        "database_status": db_status,
        "ai_status": ai_status
    }


@app.get("/webhook")
def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        return Response(content=challenge, challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


@app.post("/webhook")
async def handle_messages(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    if body.get("object") != "page":
        return {"status": "not a page object"}

    for entry in body.get("entry", []):
        for messaging_event in entry.get("messaging", []):
            background_tasks.add_task(process_webhook_event, messaging_event)
                                
    return {"status": "EVENT_RECEIVED"}

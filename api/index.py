import os
import json
import requests
from io import BytesIO
from PIL import Image
from fastapi import FastAPI, Request, Response
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from dotenv import load_dotenv

# Local testing er jonno (.env file thakle load hobe, Vercel-e eta automatic skip hoy)
load_dotenv()

app = FastAPI()

# ---- 🛠️ INITIALIZATION & CREDENTIALS ----

# Firebase Initializer (Handles Vercel environment string newline issue)
if not firebase_admin._apps:
    try:
        fb_creds_raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        fb_creds = json.loads(fb_creds_raw)
        
        # Firebase Private Key er inline '\n' format thik korar jonno
        if "private_key" in fb_creds:
            fb_creds["private_key"] = fb_creds["private_key"].replace('\\n', '\n')
            
        cred = credentials.Certificate(fb_creds)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Firebase Initialization Error: {e}")

db = firestore.client()

# Gemini AI Key Configuration
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# Facebook Tokens
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN")


# ---- 📬 MESSENGER HELPER FUNCTIONS ----

def send_fb_message(recipient_id, text):
    """Sudu normal Text Reply pathanor jonno"""
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    headers = {"Content-Type": "application/json"}
    requests.post(url, json=payload, headers=headers)


def send_product_carousel(recipient_id, products):
    """User ke Chobi, Price, Name ebong Button-shoho Card ba Carousel pathanor jonno"""
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
    
    elements = []
    for p in products:
        elements.append({
            "title": p.get('name', 'E-commerce Product'),
            "image_url": p.get('image_url', ''),  # Firebase e thaka product image link
            "subtitle": f"Price: {p.get('price', 'N/A')}\n{p.get('description', '')}",
            "buttons": [
                {
                    "type": "web_url",
                    "url": "https://yourwebsite.com/checkout", # Apnar checkout ba direct inbox link
                    "title": "Buy Now"
                }
            ]
        })
        
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements[:10]  # FB Carousel maximum 10 ta card allow kore
                }
            }
        }
    }
    headers = {"Content-Type": "application/json"}
    requests.post(url, json=payload, headers=headers)


def get_all_products():
    """Firebase Firestore theke sob product database data niye asha"""
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


# ---- 🌐 WEBHOOK ROUTING ----

@app.get("/")
def home():
    return {"status": "E-commerce Facebook Messenger Bot is Live!"}


@app.get("/webhook")
def verify_webhook(request: Request):
    """Facebook Setup er somoy token verify korar function"""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


@app.post("/webhook")
async def handle_messages(request: Request):
    """Main function jekhane messaging events ashbe"""
    body = await request.json()
    
    if body.get("object") != "page":
        return {"status": "not a page object"}

    for entry in body.get("entry", []):
        for messaging_event in entry.get("messaging", []):
            sender_id = messaging_event["sender"]["id"]
            
            # --- 📸 ১. CUSTOMER JODI PRODUCT ER CHOBI DEY ---
            if "message" in messaging_event and "attachments" in messaging_event["message"]:
                for attachment in messaging_event["message"]["attachments"]:
                    if attachment["type"] == "image":
                        image_url = attachment["payload"]["url"]
                        
                        send_fb_message(sender_id, "Apnar deya chobiti scan kora hochche, ektu opekkha korun...")
                        
                        # Firebase Database state load kora
                        all_products = get_all_products()
                        
                        try:
                            # User er chobi ti temp memory-te download kora
                            img_response = requests.get(image_url)
                            img = Image.open(BytesIO(img_response.content))
                            
                            # Gemini Vision er kache product analytics prompt
                            prompt = f"""
                            You are an expert e-commerce store manager. 
                            Compare the user's submitted image with our product inventory data here: {json.dumps(all_products)}.
                            Identify which product strictly matches the image visually.
                            Return ONLY the product 'id' string from the database object list. 
                            If absolutely no clear match is found, return 'None'.
                            Strictly do not output any sentence, markdown code, or conversational text. Just the raw 'id' or 'None'.
                            """
                            
                            # AI diye process kora
                            ai_response = model.generate_content([prompt, img])
                            matched_id = ai_response.text.strip()
                            
                            # ID match korle Firebase theke lookup kore responsive Template card pathano
                            if matched_id and matched_id != "None":
                                product_doc = db.collection('products').document(matched_id).get().to_dict()
                                send_product_carousel(sender_id, [product_doc])
                            else:
                                send_fb_message(sender_id, "Dukkhito! Ei product ti amader database-e khuje paini। Amader core collections dekhte 'onno product' likhe text korte paren।")
                        
                        except Exception as e:
                            print(f"Vision API Handling Error: {e}")
                            send_fb_message(sender_id, "Chobi ti scanning errors fash koreche। Technical internal fault, abar chesta korun।")

            # --- 💬 ২. CUSTOMER JODI TEXT MESSAGE DEY ---
            elif "message" in messaging_event and "text" in messaging_event["message"]:
                user_text = messaging_event["message"]["text"].lower()
                
                # Check user keywords (User onno product dekhte chaile)
                if any(word in user_text for word in ["product", "onno", "details", "price", "list", "collection", "notun"]):
                    products = get_all_products()
                    if products:
                        send_fb_message(sender_id, "Amader store er shera collection gulo niche deya holo:")
                        send_product_carousel(sender_id, products[:10]) # Max 10 ta card dekhabe
                    else:
                        send_fb_message(sender_id, "Amader stock e ekhon kono product empty ache। Soghrei add kora hobe।")
                
                # Normal casual chat hole AI automatic customer response handling korbe
                else:
                    try:
                        ai_chat_prompt = f"You are a helpful e-commerce sales assistant. Reply politely and briefly in English or simple Banglish/Bengali to this customer inquiry: '{user_text}'. Keep response under 2 lines."
                        ai_reply = model.generate_content(ai_chat_prompt)
                        send_fb_message(sender_id, ai_reply.text)
                    except Exception as e:
                        send_fb_message(sender_id, "Apnake kivabe sahajjo korte pari bolun? Amader product dekhte 'product' likhe text korun।")
                            
    return {"status": "EVENT_RECEIVED"}
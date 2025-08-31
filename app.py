from flask import Flask, request, jsonify
from flask_cors import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
import os

API_ID = 1778606
API_HASH = "d2bdbdd125a7e1d83fdc27c51f3791c4"

app = Flask(__name__)
CORS(app)

temp_data = {}

@app.route("/")
def home():
    return "✅ Telethon Session Generator Backend Running"

@app.route("/send_code", methods=["POST"])
async def send_code():
    data = request.json
    phone = data.get("phone")

    if not phone:
        return jsonify({"error": "Phone number is required"}), 400

    # Create a new client for the request
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone)
        
        # Save the partial session string, not the client object
        session_string = client.session.save()
        
        temp_data[phone] = {
            "session_string": session_string,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await client.disconnect()
        return jsonify({"status": "code_sent"})
        
    except Exception as e:
        # Ensure disconnection on error
        if client.is_connected():
            await client.disconnect()
        return jsonify({"error": str(e)}), 500

@app.route("/generate_session", methods=["POST"])
async def generate_session():
    data = request.json
    phone = data.get("phone")
    code = data.get("code")
    password = data.get("password")

    if not all([phone, code]):
        return jsonify({"error": "Phone number and code are required"}), 400

    if phone not in temp_data:
        return jsonify({"error": "Session expired or invalid. Please request a code again."}), 400

    stored_data = temp_data[phone]
    session_string = stored_data["session_string"]
    phone_code_hash = stored_data["phone_code_hash"]

    # Re-create the client using the saved session string
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    try:
        await client.connect()
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            await client.disconnect()
            return jsonify({"error": "2FA_password_required"}), 400
        try:
            await client.sign_in(password=password)
        except Exception as e:
            await client.disconnect()
            del temp_data[phone]
            return jsonify({"error": f"2FA Error: {str(e)}"}), 500
    except PhoneCodeInvalidError:
        await client.disconnect()
        return jsonify({"error": "Invalid verification code"}), 400
    except Exception as e:
        await client.disconnect()
        del temp_data[phone]
        return jsonify({"error": str(e)}), 500

    # On success, get the final session string
    final_session_str = client.session.save()
    await client.disconnect()
    del temp_data[phone]

    return jsonify({"session": final_session_str})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

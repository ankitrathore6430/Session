from flask import Flask, request, jsonify
from flask_cors import CORS  # Import CORS
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
import os

API_ID = 1778606
API_HASH = "d2bdbdd125a7e1d83fdc27c51f3791c4"

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# A simple in-memory dictionary to store client data between requests.
# Note: This is not suitable for a multi-instance production environment.
# A shared store like Redis would be a better choice.
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

    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent_code = await client.send_code_request(phone)

        # Store client and hash for the next step
        temp_data[phone] = {
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash
        }
        return jsonify({"status": "code_sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate_session", methods=["POST"])
async def generate_session():
    data = request.json
    phone = data.get("phone")
    code = data.get("code")
    password = data.get("password")  # Optional

    if not all([phone, code]):
        return jsonify({"error": "Phone number and code are required"}), 400

    if phone not in temp_data:
        return jsonify({"error": "Session expired or invalid. Please request a code again."}), 400

    stored_data = temp_data[phone]
    client = stored_data["client"]
    phone_code_hash = stored_data["phone_code_hash"]

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            return jsonify({"error": "2FA_password_required"}), 400
        try:
            await client.sign_in(password=password)
        except Exception as e:
            await client.disconnect()
            del temp_data[phone]
            return jsonify({"error": f"2FA Error: {str(e)}"}), 500
    except PhoneCodeInvalidError:
        return jsonify({"error": "Invalid verification code"}), 400
    except Exception as e:
        await client.disconnect()
        del temp_data[phone]
        return jsonify({"error": str(e)}), 500

    session_str = client.session.save()
    await client.disconnect()
    del temp_data[phone] # Clean up after successful login

    return jsonify({"session": session_str})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # For local development, use an ASGI server instead of app.run() for async support.
    # Example: uvicorn app:app --host 0.0.0.0 --port 5000
    app.run(host="0.0.0.0", port=port)

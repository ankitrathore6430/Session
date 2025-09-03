#!/usr/bin/env python3
"""
Telegram Multi-User Forwarder
- Users log in via /login <phone> and /otp <code> inside a chat with the controller bot.
- New: Users can also log in by uploading a .session file via /session <phone>.
- Handles 2-Step Verification (2FA) with a /password <password> function.
- Each user's session file is saved automatically (<phone>.session).
- Each user's source/target groups are saved in users.json and restored on restart.
- Functions available (for each logged-in account, used inside groups):
    /add_source [id/username] -> mark a group as a source
    /add_target [id/username] -> mark a group as a target
    /list_source    -> list saved source group IDs
    /list_target    -> list saved target group IDs
    /remove_source [id/username] -> remove current group or specified group from sources
    /remove_target [id/username] -> remove current group or specified group from targets
    /getgroups      -> list all joined groups and channels
    /restart_forwarding       -> resumes message forwarding
    /stop_forwarding          -> pauses message forwarding
    /auto_message <min|stop> -> auto-forward last message from first source
    /delaytime <min> <max> -> Set custom delay between forwards.
    /logout         -> (run in private chat with controller) log out a phone/session
- Admin Panel Features:
    /admin          -> Shows an interactive button-based admin panel.
"""

import os
import asyncio
import json
import random
import re
import functools
import struct
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events, errors, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import StartBotRequest
import qrcode
import io

# --- Flask App for Uptime Monitoring ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def start_flask_thread():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

# --- Configuration ---
# WARNING: Your credentials are hard-coded below.
# This is NOT a secure practice. Avoid sharing this file.
API_ID = 1778606
API_HASH = "d2bdbdd125a7e1d83fdc27c51f3791c4"
BOT_TOKEN = "8328669216:AAG6GnZxUQzdJwIii43WTlrIesIxGE10LLs"
ADMIN_ID = 745211839

# Default delay configuration to avoid session termination
MIN_DELAY = 5
MAX_DELAY = 15
# --- End of configuration ---

DATA_FILE = "users.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {} # Return empty dict if file is corrupted
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

user_data = load_data()

# State-tracking dictionaries
pending_logins = {}
pending_passwords = {}
pending_sessions = {}
clients = {}
logins_in_progress = set()
auto_forward_tasks = {} # Tracks auto-forwarding tasks
admin_states = {}

controller = TelegramClient("bot", API_ID, API_HASH)


# NEW FUNCTION: Handles client disconnection and cleanup
async def run_and_cleanup_client(phone, client):
    """Runs the client and ensures it's removed from the active list upon disconnection."""
    try:
        await client.run_until_disconnected()
    finally:
        print(f"[{phone}] Client disconnected. Removing from active clients dictionary.")
        clients.pop(phone, None)

async def complete_login_and_start_client(event, phone, client):
    """Finalizes the login process and starts the client services."""
    me = await client.get_me()
    await event.respond(f"✅ Logged in as {me.first_name} ({phone}). Sessions saved.")
    user_data.setdefault(phone, {"sources": [], "targets": []})
    user_data[phone]['controller_id'] = event.sender_id
    user_data[phone].setdefault('forwarding_enabled', True)
    save_data(user_data)
    clients[phone] = client
    # User client now only needs the forwarder handler
    client.add_event_handler(
        functools.partial(handle_forwarder, phone=phone),
        events.NewMessage()
    )
    await resume_auto_forwarder_if_needed(phone, client)

    if phone in logins_in_progress:
        logins_in_progress.remove(phone)
    
    # MODIFIED: Use the new cleanup wrapper
    asyncio.create_task(run_and_cleanup_client(phone, client))


# --- Auto-Forwarder & User Client Logic ---

async def auto_forwarder_task(phone: str, client: TelegramClient):
    """The background task that periodically forwards messages."""
    while True:
        user_config = user_data.get(phone, {})
        if not user_config.get('auto_forward_enabled', False):
            break

        interval = user_config.get('auto_forward_interval', 0)
        sources = user_config.get('sources', [])
        targets = user_config.get('targets', [])

        if not all([interval, sources, targets]):
            print(f"[{phone}] Auto-forwarding paused: Missing interval, source, or target.")
            await asyncio.sleep(300)
            continue

        try:
            print(f"[{phone}] Auto-forwarder running. Interval: {interval} min.")
            first_source = sources[0]
            messages = await client.get_messages(first_source, limit=1)
            if not messages:
                print(f"[{phone}] No messages found in source {first_source}.")
                await asyncio.sleep(interval * 60)
                continue
            
            min_d = user_config.get('min_delay', MIN_DELAY)
            max_d = user_config.get('max_delay', MAX_DELAY)

            last_message = messages[0]
            for target in targets:
                try:
                    await client.send_message(target, last_message)
                    delay = random.randint(min_d, max_d)
                    print(f"[{phone}] Auto-forwarded message to {target}. Waiting for {delay}s.")
                    await asyncio.sleep(delay)
                except Exception as e:
                    print(f"[{phone}] Auto-forward error to target {target}: {e}")

        except Exception as e:
            print(f"[{phone}] Error in auto-forwarder task: {e}")

        await asyncio.sleep(interval * 60)

    if phone in auto_forward_tasks:
        del auto_forward_tasks[phone]
    print(f"[{phone}] Auto-forwarder task has stopped.")

async def handle_forwarder(event, phone: str):
    if not user_data.get(phone, {}).get("forwarding_enabled", True):
        return

    client = event.client
    if event.chat_id in user_data[phone]["sources"]:
        user_config = user_data.get(phone, {})
        min_d = user_config.get('min_delay', MIN_DELAY)
        max_d = user_config.get('max_delay', MAX_DELAY)
        
        for tgt in list(user_data[phone]["targets"]):
            try:
                await client.send_message(tgt, event.message)
                delay = random.randint(min_d, max_d)
                print(f"[{phone}] Forwarded message to {tgt}. Waiting for {delay}s.")
                await asyncio.sleep(delay)
            except Exception as e:
                print(f"[{phone}] Forward error to {tgt}: {e}")

# --- Helper to get user's client ---
async def get_user_client_from_event(event):
    """Helper function to find the user's phone and active client."""
    user_phones = [p for p, d in user_data.items() if d.get('controller_id') == event.sender_id]
    if not user_phones:
        await event.respond("You do not have any accounts logged in. Please use /login first.")
        return None, None
    
    phone = user_phones[0]
    client = clients.get(phone)
    
    if not client or not client.is_connected():
        await event.respond(f"The client for `{phone}` is not currently active. It may still be connecting. Please try again shortly.")
        return None, None
        
    return phone, client

# --- START: Controller Bot Handlers for User Commands ---

@controller.on(events.NewMessage(pattern=re.compile(r"^/delaytime(.*)$")))
async def handle_delay_time(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return

    parts = event.raw_text.split()
    user_config = user_data.get(phone, {})
    current_min = user_config.get('min_delay', MIN_DELAY)
    current_max = user_config.get('max_delay', MAX_DELAY)

    if len(parts) != 3:
        await event.respond(
            f"ℹ️ **Current Delay:** Your messages are forwarded with a random delay between **{current_min}** and **{current_max}** seconds.\n\n"
            "**To change this, use:**\n`/delaytime <min_seconds> <max_seconds>`\n\n"
            "*Example:* `/delaytime 10 30`",
            parse_mode='md'
        )
        return

    try:
        min_val = int(parts[1])
        max_val = int(parts[2])
        if min_val < 0 or max_val < 0:
            await event.respond("❌ Delay values cannot be negative.")
            return
        if min_val > max_val:
            await event.respond("❌ Minimum delay cannot be greater than maximum delay.")
            return

        user_data[phone]['min_delay'] = min_val
        user_data[phone]['max_delay'] = max_val
        save_data(user_data)
        await event.respond(f"✅ Delay time has been set to a random interval between **{min_val}** and **{max_val}** seconds.", parse_mode='md')
    except ValueError:
        await event.respond("❌ Invalid input. Please provide two numbers for the delay range (e.g., `/delaytime 5 20`).")

@controller.on(events.NewMessage(pattern=re.compile(r"^/auto_message(\s+.+)?$")))
async def handle_auto_message_command(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return

    parts = event.raw_text.split(maxsplit=1)
    arg = parts[1].lower() if len(parts) == 2 else ""

    if arg == "stop":
        user_data[phone]['auto_forward_enabled'] = False
        save_data(user_data)
        if phone in auto_forward_tasks:
            auto_forward_tasks[phone].cancel()
        await event.respond("🛑 Auto-messaging has been **stopped**.")
    elif arg.isdigit():
        minutes = int(arg)
        if minutes <= 0:
            return await event.respond("❌ The time in minutes must be greater than 0.")

        user_data[phone]['auto_forward_interval'] = minutes
        user_data[phone]['auto_forward_enabled'] = True
        save_data(user_data)

        if phone in auto_forward_tasks:
            auto_forward_tasks[phone].cancel()

        task = asyncio.create_task(auto_forwarder_task(phone, client))
        auto_forward_tasks[phone] = task
        await event.respond(f"✅ Auto-messaging started from the first source group every **{minutes}** minutes.")
    else:
        await event.respond("**Auto-Message Usage:**\n`/auto_message <minutes>` - Start auto-messaging\n`/auto_message stop` - Stop auto-messaging")

@controller.on(events.NewMessage(pattern=re.compile(r"^/add_source(\s+.+)?$")))
async def handle_add_source(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
    
    parts = event.raw_text.split(maxsplit=1)
    entity_identifier = parts[1] if len(parts) == 2 else None
    if entity_identifier:
        try:
            entity = await client.get_entity(int(entity_identifier) if entity_identifier.lstrip('-').isdigit() else entity_identifier)
            cid = entity.id
            if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
                if not str(cid).startswith("-100"): cid = int(f"-100{cid}")

            if cid not in user_data[phone]["sources"]:
                user_data[phone]["sources"].append(cid)
                save_data(user_data)
            title = getattr(entity, 'title', getattr(entity, 'username', entity_identifier))
            await event.respond(f"✅ '{title}' is saved as a SOURCE.")
        except Exception as e:
            await event.respond(f"❌ Could not find chat '{entity_identifier}'. Error: {e}")
    else:
        await event.respond("❌ This command requires a chat ID or username.\n*Example:* `/add_source @channelname`")

@controller.on(events.NewMessage(pattern=re.compile(r"^/add_target(\s+.+)?$")))
async def handle_add_target(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
        
    parts = event.raw_text.split(maxsplit=1)
    entity_identifier = parts[1] if len(parts) == 2 else None
    if entity_identifier:
        try:
            entity = await client.get_entity(int(entity_identifier) if entity_identifier.lstrip('-').isdigit() else entity_identifier)
            cid = entity.id
            if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
                if not str(cid).startswith("-100"): cid = int(f"-100{cid}")

            if cid not in user_data[phone]["targets"]:
                user_data[phone]["targets"].append(cid)
                save_data(user_data)
            title = getattr(entity, 'title', getattr(entity, 'username', entity_identifier))
            await event.respond(f"✅ '{title}' is saved as a TARGET.")
        except Exception as e:
            await event.respond(f"❌ Could not find chat '{entity_identifier}'. Error: {e}")
    else:
        await event.respond("❌ This command requires a chat ID or username.\n*Example:* `/add_target @channelname`")

@controller.on(events.NewMessage(pattern=re.compile(r"^/list_source$")))
async def handle_list_sources(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
        
    source_ids = user_data[phone].get("sources", [])
    if not source_ids:
        return await event.respond("📚 **Sources:**\n— none —", parse_mode='md')

    await event.respond("📚 **Your Sources:**")
    for sid in source_ids:
        try:
            entity = await client.get_entity(sid)
            title = entity.title
            username = f"@{entity.username}" if hasattr(entity, 'username') and entity.username else "N/A"
            button = Button.inline("➖ Remove Source", data=f"rem_so_{sid}")
            message = f"**Name:** {title}\n**Group ID:** `{sid}`\n**Username:** {username}"
            await event.respond(message, buttons=button, parse_mode='md')
        except Exception:
            button = Button.inline("➖ Remove Source", data=f"rem_so_{sid}")
            message = f"**Name:** Unknown or Inaccessible Chat\n**Group ID:** `{sid}`\n**Username:** N/A"
            await event.respond(message, buttons=button, parse_mode='md')

@controller.on(events.NewMessage(pattern=re.compile(r"^/list_target$")))
async def handle_list_targets(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
    
    target_ids = user_data[phone].get("targets", [])
    if not target_ids:
        return await event.respond("🎯 **Targets:**\n— none —", parse_mode='md')

    await event.respond("🎯 **Your Targets:**")
    for tid in target_ids:
        try:
            entity = await client.get_entity(tid)
            title = entity.title
            username = f"@{entity.username}" if hasattr(entity, 'username') and entity.username else "N/A"
            button = Button.inline("➖ Remove Target", data=f"rem_ta_{tid}")
            message = f"**Name:** {title}\n**Group ID:** `{tid}`\n**Username:** {username}"
            await event.respond(message, buttons=button, parse_mode='md')
        except Exception:
            button = Button.inline("➖ Remove Target", data=f"rem_ta_{tid}")
            message = f"**Name:** Unknown or Inaccessible Chat\n**Group ID:** `{tid}`\n**Username:** N/A"
            await event.respond(message, buttons=button, parse_mode='md')

@controller.on(events.NewMessage(pattern=re.compile(r"^/remove_source(\s+.+)?$")))
async def handle_remove_source(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
    
    parts = event.raw_text.split(maxsplit=1)
    entity_identifier = parts[1] if len(parts) == 2 else None

    if entity_identifier:
        try:
            entity = await client.get_entity(int(entity_identifier) if entity_identifier.lstrip('-').isdigit() else entity_identifier)
            title = getattr(entity, 'title', getattr(entity, 'username', entity_identifier))
            raw_id = entity.id
            prefixed_id = int(f"-100{raw_id}") if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup') else raw_id
            removed = False
            if raw_id in user_data[phone]["sources"]:
                user_data[phone]["sources"].remove(raw_id)
                removed = True
            if prefixed_id in user_data[phone]["sources"]:
                user_data[phone]["sources"].remove(prefixed_id)
                removed = True
            if removed:
                save_data(user_data)
                await event.respond(f"🧹 Removed '{title}' from SOURCES.")
            else:
                await event.respond(f"ℹ️ '{title}' is not in your SOURCES.")
        except Exception as e:
            await event.respond(f"❌ Could not find chat '{entity_identifier}'. Error: {e}")
    else:
        await event.respond("❌ This command requires a chat ID or username.\n*Example:* `/remove_source @channelname`")

@controller.on(events.NewMessage(pattern=re.compile(r"^/remove_target(\s+.+)?$")))
async def handle_remove_target(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
    
    parts = event.raw_text.split(maxsplit=1)
    entity_identifier = parts[1] if len(parts) == 2 else None

    if entity_identifier:
        try:
            entity = await client.get_entity(int(entity_identifier) if entity_identifier.lstrip('-').isdigit() else entity_identifier)
            title = getattr(entity, 'title', getattr(entity, 'username', entity_identifier))
            raw_id = entity.id
            prefixed_id = int(f"-100{raw_id}") if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup') else raw_id
            removed = False
            if raw_id in user_data[phone]["targets"]:
                user_data[phone]["targets"].remove(raw_id)
                removed = True
            if prefixed_id in user_data[phone]["targets"]:
                user_data[phone]["targets"].remove(prefixed_id)
                removed = True
            if removed:
                save_data(user_data)
                await event.respond(f"🧹 Removed '{title}' from TARGETS.")
            else:
                await event.respond(f"ℹ️ '{title}' is not in your TARGETS.")
        except Exception as e:
            await event.respond(f"❌ Could not find chat '{entity_identifier}'. Error: {e}")
    else:
        await event.respond("❌ This command requires a chat ID or username.\n*Example:* `/remove_target @channelname`")

@controller.on(events.NewMessage(pattern=re.compile(r"^/getgroups$")))
async def handle_get_groups(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return

    response_msg = await event.respond("🔄 Fetching your group list, please wait...")
    try:
        dialog_count = 0
        sent_header = False
        async for dialog in client.iter_dialogs():
            dialog_count += 1
            if not sent_header:
                await response_msg.edit("📚 **Your Joined Groups & Channels:** (Click to add)")
                sent_header = True

            if dialog.is_group or dialog.is_channel:
                title = dialog.title
                chat_id = dialog.entity.id
                username = f"@{dialog.entity.username}" if hasattr(dialog.entity, 'username') and dialog.entity.username else "N/A"
                buttons = [
                    Button.inline("➕ Add Source", data=f"add_so_{chat_id}"),
                    Button.inline("➕ Add Target", data=f"add_ta_{chat_id}")
                ]
                message = f"**Name:** {title}\n**Group ID:** `{chat_id}`\n**Username:** {username}"
                await event.respond(message, buttons=buttons, parse_mode='md')
                await asyncio.sleep(0.2)  # Prevents hitting flood limits

        if dialog_count == 0:
            await response_msg.edit("ℹ️ You have not joined any groups or channels.")
        else:
            if not sent_header:
                 await response_msg.edit("ℹ️ No groups or channels found.")
            else:
                await response_msg.delete()
                await event.respond("✅ **Finished listing all groups and channels.**")

    except Exception as e:
        await response_msg.edit(f"❌ An error occurred while fetching your groups: {e}")

@controller.on(events.NewMessage(pattern=re.compile(r"^/restart_forwarding$")))
async def handle_start_forwarding(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return
    
    user_data[phone]['forwarding_enabled'] = True
    save_data(user_data)
    await event.respond("▶️ Forwarding has been **started**.")

@controller.on(events.NewMessage(pattern=re.compile(r"^/stop_forwarding$")))
async def handle_stop_forwarding(event):
    if not event.is_private: return
    phone, client = await get_user_client_from_event(event)
    if not client: return

    user_data[phone]['forwarding_enabled'] = False
    save_data(user_data)
    await event.respond("⏸️ Forwarding has been **stopped**.")

# --- Startup Logic ---

async def resume_auto_forwarder_if_needed(phone: str, client: TelegramClient):
    user_config = user_data.get(phone, {})
    if user_config.get('auto_forward_enabled', False):
        print(f"[{phone}] Resuming auto-forwarder task.")
        task = asyncio.create_task(auto_forwarder_task(phone, client))
        auto_forward_tasks[phone] = task

async def start_or_resume_client(phone: str, notify_user_id=None):
    client = TelegramClient(phone, API_ID, API_HASH, device_model="PC 64bit", system_version="Windows 10", app_version="1.0", lang_code="en", system_lang_code="en")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise Exception("The session file is invalid or expired.")

        me = await client.get_me()
        print(f"✅ Active: {me.first_name} ({phone})")
        clients[phone] = client

        user_data.setdefault(phone, {"sources": [], "targets": []})
        if notify_user_id:
            user_data[phone]['controller_id'] = notify_user_id
        save_data(user_data)

        client.add_event_handler(
            functools.partial(handle_forwarder, phone=phone),
            events.NewMessage()
        )
        await resume_auto_forwarder_if_needed(phone, client)

        if notify_user_id:
            await controller.send_message(notify_user_id, f"✅ **Session Validated!**\nSuccessfully logged in as **{me.first_name}** (`{phone}`). The client is now running.", parse_mode='md')
        
        # MODIFIED: Use the new cleanup wrapper
        asyncio.create_task(run_and_cleanup_client(phone, client))

    except Exception as e:
        print(f"❌ Failed to start client for {phone}: {e}")
        if notify_user_id:
            await controller.send_message(notify_user_id, f"❌ **Login Failed!**\nCould not start the client for `{phone}`.\n\n**Reason:** {e}", parse_mode='md')

        session_file = f"{phone}.session"
        if os.path.exists(session_file):
            os.remove(session_file)

# --- START: Admin Panel and Conversation Logic ---

@controller.on(events.NewMessage(from_users=ADMIN_ID, func=lambda e: e.is_private))
async def admin_conversation_handler(event):
    state = admin_states.get(event.sender_id)
    if not state: return
    admin_states.pop(event.sender_id)
    message_to_delete = event.message

    if state == 'awaiting_broadcast_message':
        await message_to_delete.delete()
        status_msg = await event.respond("📢 Broadcasting your message to all users...")
        all_controller_ids = {d['controller_id'] for d in user_data.values() if 'controller_id' in d}
        if not all_controller_ids: return await status_msg.edit("No users to broadcast to.")
        success_count, fail_count = 0, 0
        for user_id in all_controller_ids:
            try:
                await controller.send_message(user_id, event.message)
                success_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Broadcast failed for user {user_id}: {e}")
                fail_count += 1
        await status_msg.edit(f"✅ **Broadcast Complete**\nSent: {success_count} | Failed: {fail_count}")
    elif state == 'awaiting_delete_user_id':
        identifier = event.raw_text
        phone_to_delete = None
        if identifier.startswith('+') and all(c.isdigit() for c in identifier[1:]):
            if identifier in user_data: phone_to_delete = identifier
        elif identifier.isdigit():
            user_id_to_find = int(identifier)
            for phone, data in user_data.items():
                if data.get('controller_id') == user_id_to_find:
                    phone_to_delete = phone
                    break
        if not phone_to_delete: await event.respond(f"❌ Could not find a user with the identifier: `{identifier}`.")
        else:
            result = await perform_logout(phone_to_delete)
            await event.respond(result['message'], parse_mode='md')
    elif state == 'awaiting_forcejoin_channel':
        entity_to_join = event.raw_text
        if not clients: return await event.respond("No active user clients to perform the action.")
        status_msg = await event.respond(f"🚀 Instructing {len(clients)} active users to interact with `{entity_to_join}`...")
        success_count, fail_count = 0, 0
        for phone, client in clients.items():
            try:
                entity = await client.get_entity(entity_to_join)
                action_taken = ""
                if getattr(entity, 'bot', False):
                    await client(StartBotRequest(bot=entity, peer=entity, random_id=random.randint(0, 2**63 - 1), start_param=""))
                    action_taken = "Started bot"
                elif getattr(entity, 'megagroup', False) or getattr(entity, 'broadcast', False):
                    await client(JoinChannelRequest(entity))
                    action_taken = "Joined"
                else:
                    fail_count += 1
                    print(f"[{phone}] Could not join '{entity_to_join}': Unsupported entity type.")
                    continue
                success_count += 1
                print(f"[{phone}] Successfully: {action_taken} '{entity_to_join}'")
            except Exception as e:
                fail_count += 1
                print(f"[{phone}] Failed to interact with '{entity_to_join}': {e}")
            await asyncio.sleep(1)
        await status_msg.edit(f"✅ **Force Action Complete**\nTarget: `{entity_to_join}`\nSuccess: {success_count} | Failed: {fail_count}", parse_mode='md')
    elif state == 'awaiting_download_user_id':
        identifier = event.raw_text
        phone_to_find = None
        if identifier.startswith('+') and identifier in user_data: phone_to_find = identifier
        elif identifier.isdigit():
            user_id_to_find = int(identifier)
            for phone, data in user_data.items():
                if data.get('controller_id') == user_id_to_find:
                    phone_to_find = phone
                    break
        if not phone_to_find:
            await event.respond(f"❌ Could not find a user with the identifier: `{identifier}`.")
            raise events.StopPropagation
        session_filename = f"{phone_to_find}.session"
        if os.path.exists(session_filename):
            await event.respond(f"📥 Found session for `{phone_to_find}`. Uploading file...")
            await event.client.send_file(event.chat_id, session_filename, caption=f"Here is the session file for user `{phone_to_find}`.")
        else:
            await event.respond(f"❌ Session file for `{phone_to_find}` does not exist. The user may have logged out.")
    raise events.StopPropagation

@controller.on(events.NewMessage(pattern=re.compile(r"^/admin$")))
async def admin_panel_function(event):
    if event.sender_id != ADMIN_ID: return
    admin_buttons = [[Button.inline("👥 List Users", data="list_users"), Button.inline("📢 Broadcast", data="broadcast")], [Button.inline("🗑️ Delete User", data="delete_user"), Button.inline("🚀 Force Join", data="forcejoin")], [Button.inline("📥 Download Session", data="download_session")]]
    await event.respond("👑 **Admin Panel**\n\nSelect an option from the menu below:", buttons=admin_buttons, parse_mode='md')
    raise events.StopPropagation

@controller.on(events.CallbackQuery)
async def main_button_handler(event):
    data_str = event.data.decode('utf-8')

    # --- Admin Button Logic ---
    if event.sender_id == ADMIN_ID and not data_str.startswith(('rem_', 'add_')):
        data = data_str
        cancel_button = Button.inline("❌ Cancel", "cancel_admin_action")
        if data == 'list_users':
            await event.answer("Fetching user list...")
            if not user_data: return await event.respond("No users have logged in yet.")
            message = "👥 **List of All Users**\n\n"
            for phone, u_data in user_data.items():
                controller_id = u_data.get('controller_id', 'N/A')
                forwarding_status = "ON" if u_data.get('forwarding_enabled', True) else "OFF"
                session_status = "✅ Active" if phone in clients and clients[phone].is_connected() else "❌ Inactive"
                message += f"• **Phone:** `{phone}`\n  **Controller ID:** `{controller_id}`\n  **Forwarding:** {forwarding_status}\n  **Session:** {session_status}\n\n"
            if len(message) > 4096:
                await event.respond("The user list is too long, sending as a file.")
                with io.BytesIO(message.encode('utf-8')) as f:
                    f.name = "user_list.txt"
                    await event.client.send_file(event.chat_id, f, caption="User List")
            else: await event.respond(message, parse_mode='md')
        elif data == 'broadcast':
            admin_states[ADMIN_ID] = 'awaiting_broadcast_message'
            await event.edit("📢 **Broadcast Mode**\n\nPlease send the message you want to broadcast to all users.", buttons=cancel_button, parse_mode='md')
        elif data == 'delete_user':
            admin_states[ADMIN_ID] = 'awaiting_delete_user_id'
            await event.edit("🗑️ **Delete User Mode**\n\nPlease send the phone number or user ID of the user you want to delete.", buttons=cancel_button, parse_mode='md')
        elif data == 'forcejoin':
            admin_states[ADMIN_ID] = 'awaiting_forcejoin_channel'
            await event.edit("🚀 **Force Join Mode**\n\nPlease send the username of the channel/group/bot for all accounts to join (e.g., @telegram).", buttons=cancel_button, parse_mode='md')
        elif data == 'download_session':
            admin_states[ADMIN_ID] = 'awaiting_download_user_id'
            await event.edit("📥 **Download Session**\n\nPlease send the phone number or user ID of the account you want to download.", buttons=cancel_button, parse_mode='md')
        elif data == 'cancel_admin_action':
            admin_states.pop(ADMIN_ID, None)
            admin_buttons = [[Button.inline("👥 List Users", data="list_users"), Button.inline("📢 Broadcast", data="broadcast")], [Button.inline("🗑️ Delete User", data="delete_user"), Button.inline("🚀 Force Join", data="forcejoin")], [Button.inline("📥 Download Session", data="download_session")]]
            await event.edit("👑 **Admin Panel**\n\nAction cancelled. Select an option:", buttons=admin_buttons, parse_mode='md')
        return

    # --- User Button Logic ---
    phone, client = await get_user_client_from_event(event)
    if not client: return await event.answer("Your client is not active.", alert=True)

    try:
        parts = data_str.split('_', 2)
        action, entity_type, entity_id_str = parts[0], parts[1], parts[2]
        entity_id = int(entity_id_str)

        if action == 'rem':
            original_message = await event.get_message()
            if entity_type == 'so' and entity_id in user_data[phone]['sources']:
                user_data[phone]['sources'].remove(entity_id)
                await event.edit(f"~~{original_message.text}~~\n*Source Removed.*", parse_mode='md', buttons=None)
                await event.answer("Source removed!", alert=True)
            elif entity_type == 'ta' and entity_id in user_data[phone]['targets']:
                user_data[phone]['targets'].remove(entity_id)
                await event.edit(f"~~{original_message.text}~~\n*Target Removed.*", parse_mode='md', buttons=None)
                await event.answer("Target removed!", alert=True)
            save_data(user_data)
        elif action == 'add':
            entity = await client.get_entity(entity_id)
            id_to_save = entity.id
            if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
                if not str(id_to_save).startswith("-100"):
                    id_to_save = int(f"-100{id_to_save}")
            
            if entity_type == 'so':
                if id_to_save not in user_data[phone]['sources']:
                    user_data[phone]['sources'].append(id_to_save)
                    save_data(user_data)
                    await event.answer("✅ Added as a source!", alert=True)
                else: await event.answer("Already a source.", alert=True)
            elif entity_type == 'ta':
                if id_to_save not in user_data[phone]['targets']:
                    user_data[phone]['targets'].append(id_to_save)
                    save_data(user_data)
                    await event.answer("✅ Added as a target!", alert=True)
                else: await event.answer("Already a target.", alert=True)
    except (ValueError, IndexError):
        await event.answer("Invalid button data.", alert=True)


# --- START: Controller Bot Handlers for Login and Core Functions ---

@controller.on(events.NewMessage(pattern=re.compile(r"^/login(\s+.+)?$")))
async def login_function(event):
    if not event.is_private: return
    parts = event.raw_text.split()
    if len(parts) != 2:
        return await event.respond("Usage: `/login <phone_number>`", parse_mode='md')
    phone = parts[1]
    if phone in logins_in_progress:
        return await event.respond("A login for this number is already in progress. Please wait or cancel.")
    if phone in clients and clients.get(phone) and clients.get(phone).is_connected():
        return await event.respond("This number is already logged in and active.")
    client = None
    try:
        logins_in_progress.add(phone)
        client = TelegramClient(phone, API_ID, API_HASH, device_model="PC 64bit", system_version="Windows 10", app_version="1.0", lang_code="en", system_lang_code="en")
        await client.connect()
        qr_login = await client.qr_login()
        pending_logins[event.sender_id] = {"phone": phone, "client": client, "qr_login": qr_login}
        qr_img = qrcode.make(qr_login.url)
        temp_file_path = f"qr_{event.sender_id}.png"
        qr_img.save(temp_file_path, format="PNG")
        await event.client.send_file(event.chat_id, temp_file_path, caption="Scan this QR code with your Telegram app:")
        os.remove(temp_file_path)
        try:
            await asyncio.wait_for(qr_login.wait(), timeout=300)
        except asyncio.TimeoutError:
            if phone in logins_in_progress:
                logins_in_progress.remove(phone)
            await client.disconnect()
            await event.respond("❌ Login expired: The QR code was not scanned in time. Please try again.")
            return
        await complete_login_and_start_client(event, phone, client)
    except errors.SessionPasswordNeededError:
        pending_passwords[event.sender_id] = {"phone": phone, "client": client}
        await event.respond("🔑 Your account has 2FA enabled. Please reply with `/password <your_password>`", parse_mode='md')
    except Exception as e:
        if phone in logins_in_progress:
            logins_in_progress.remove(phone)
        if client: await client.disconnect()
        await event.respond(f"❌ Login error: {e}")

@controller.on(events.NewMessage(pattern=re.compile(r"^/loginotp(\s+.+)?$")))
async def login_otp_function(event):
    if not event.is_private: return
    parts = event.raw_text.split()
    if len(parts) != 2:
        return await event.respond("Usage: `/loginotp <phone_number>`", parse_mode='md')
    phone = parts[1]
    if phone in logins_in_progress:
        return await event.respond("A login for this number is already in progress. Please complete it or wait.")
    if phone in clients and clients.get(phone) and clients.get(phone).is_connected():
        return await event.respond("This number is already logged in and active.")
    client = None
    try:
        logins_in_progress.add(phone)
        client = TelegramClient(phone, API_ID, API_HASH, device_model="PC 64bit", system_version="Windows 10", app_version="1.0", lang_code="en", system_lang_code="en")
        await client.connect()
        sent_code = await client.send_code_request(phone)
        pending_logins[event.sender_id] = {"phone": phone, "client": client, "sent_code": sent_code}
        await event.respond("✅ A login code has been sent to your Telegram app. Please reply with `/otp <code>`", parse_mode='md')
    except Exception as e:
        if phone in logins_in_progress:
            logins_in_progress.remove(phone)
        if client: await client.disconnect()
        await event.respond(f"❌ Login error: {e}")

@controller.on(events.NewMessage(pattern=re.compile(r"^/session(\s+.+)?$")))
async def session_login_start(event):
    if not event.is_private: return
    parts = event.raw_text.split()
    if len(parts) != 2:
        return await event.respond("Usage: `/session <phone_number>`\n\n*Example: `/session +1234567890`*", parse_mode='md')
    phone = parts[1]
    if phone in clients and clients.get(phone) and clients.get(phone).is_connected():
        return await event.respond("This number is already logged in and active.")
    pending_sessions[event.sender_id] = phone
    await event.respond(f"✅ Ready to receive session file for `{phone}`.\n\nPlease upload the `{phone}.session` file now.", parse_mode='md')

@controller.on(events.NewMessage(pattern=re.compile(r"^/session_string(\s+.+)?$")))
async def session_string_login(event):
    if not event.is_private: return
    parts = event.raw_text.split(maxsplit=2)
    if len(parts) != 3:
        return await event.respond("Usage: `/session_string <phone_number> <session_string>`", parse_mode='md')

    phone = parts[1]
    session_string = parts[2]

    if phone in clients and clients.get(phone) and clients.get(phone).is_connected():
        return await event.respond("This number is already logged in and active.")
    if phone in logins_in_progress:
        return await event.respond("A login for this number is already in progress. Please wait.")

    temp_client = None
    try:
        temp_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

        status_msg = await event.respond("⏳ Validating session string...")
        await temp_client.connect()
        
        if not await temp_client.is_user_authorized():
            await status_msg.edit("❌ The provided session string is invalid or expired. Please generate a new one.")
            return

        session_filename = f"{phone}.session"
        persistent_client = TelegramClient(session_filename, API_ID, API_HASH)
        await persistent_client.connect()
        
        persistent_client.session.set_dc(
            temp_client.session.dc_id,
            temp_client.session.server_address,
            temp_client.session.port
        )
        persistent_client.session.auth_key = temp_client.session.auth_key
        persistent_client.session.takeout_id = temp_client.session.takeout_id
        
        persistent_client.session.save()
        await persistent_client.disconnect()

        await status_msg.edit(f"✅ Session string validated and saved for `{phone}`. Starting client...", parse_mode='md')
        asyncio.create_task(start_or_resume_client(phone, notify_user_id=event.sender_id))

    except struct.error:
        await event.respond(
            "❌ **Invalid Session String Format.**\n\n"
            "The string you provided is not a valid Telethon session string. "
            "Please ensure you are copying the entire string generated by a session bot or script.",
            parse_mode='md'
        )
    except Exception as e:
        await event.respond(f"❌ An error occurred: `{e}`")
    finally:
        if temp_client and temp_client.is_connected():
            await temp_client.disconnect()

@controller.on(events.NewMessage(func=lambda e: e.document and e.is_private))
async def session_login_receive(event):
    sender_id = event.sender_id
    if sender_id not in pending_sessions: return
    phone = pending_sessions[sender_id]
    file_name = event.file.name if event.file and hasattr(event.file, 'name') else "unknown_file"
    if file_name != f"{phone}.session":
        return await event.respond(f"❌ **Invalid Filename.**\nExpected `{phone}.session` but received `{file_name}`.", parse_mode='md')
    try:
        del pending_sessions[sender_id]
        await event.download_media(file=f'./{file_name}')
        await event.respond(f"✅ Session file received. Trying to log in...")
        asyncio.create_task(start_or_resume_client(phone, notify_user_id=sender_id))
    except Exception as e:
        await event.respond(f"❌ An error occurred while processing the session file: {e}")

@controller.on(events.NewMessage(pattern=re.compile(r"^/otp(\s+.+)?$")))
async def otp_function(event):
    if not event.is_private: return
    parts = event.raw_text.split()
    if len(parts) != 2:
        return await event.respond("Usage: `/otp <code>`", parse_mode='md')
    sender_id = event.sender_id
    if sender_id not in pending_logins:
        return await event.respond("❌ No pending login. Please start with `/loginotp <phone_number>`", parse_mode='md')
    login_state = pending_logins.pop(sender_id)
    client, phone, sent_code = login_state["client"], login_state["phone"], login_state["sent_code"]
    code = parts[1]
    try:
        await client.sign_in(phone=phone, code=code, password=None)
        await complete_login_and_start_client(event, phone, client)
    except errors.SessionPasswordNeededError:
        pending_passwords[event.sender_id] = {"phone": phone, "client": client}
        await event.respond("🔑 Your account has 2FA enabled. Please reply with `/password <your_password>`", parse_mode='md')
    except Exception as e:
        if phone in logins_in_progress:
            logins_in_progress.remove(phone)
        await client.disconnect()
        await event.respond(f"❌ Login error: {e}")

@controller.on(events.NewMessage(pattern=re.compile(r"^/password(\s+.+)?$")))
async def password_function(event):
    if not event.is_private: return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) != 2:
        return await event.respond("Usage: `/password <your_password>`", parse_mode='md')
    sender_id = event.sender_id
    if sender_id not in pending_passwords:
        return await event.respond("❌ No pending 2FA login. Please start with a login command first.", parse_mode='md')
    password_state = pending_passwords.pop(sender_id)
    client, phone = password_state["client"], password_state["phone"]
    password = parts[1]
    try:
        await client.sign_in(password=password)
        await complete_login_and_start_client(event, phone, client)
    except Exception as e:
        if phone in logins_in_progress:
            logins_in_progress.remove(phone)
        await client.disconnect()
        await event.respond(f"❌ Login error: {e}")

async def perform_logout(phone_to_delete: str):
    """Helper function to perform logout and data cleanup for a given phone number."""
    response = {}
    is_active_client = phone_to_delete in clients
    has_session_file = os.path.exists(f"{phone_to_delete}.session")
    has_user_data = phone_to_delete in user_data
    if not is_active_client and not has_session_file and not has_user_data:
        response['message'] = f"ℹ️ No data or active session found for `{phone_to_delete}`."
        return response
    try:
        client = clients.pop(phone_to_delete, None)
        if client:
            await client.log_out()
            await client.disconnect()
            print(f"[{phone_to_delete}] Active client has been logged out and disconnected.")
        if phone_to_delete in user_data:
            del user_data[phone_to_delete]
            save_data(user_data)
            print(f"[{phone_to_delete}] Data has been removed from users.json.")
        session_file = f"{phone_to_delete}.session"
        if os.path.exists(session_file):
            os.remove(session_file)
            print(f"[{phone_to_delete}] Session file has been removed.")
        if phone_to_delete in logins_in_progress:
            logins_in_progress.remove(phone_to_delete)
        response['message'] = f"✅ **Logout Complete.**\nAll data and sessions for `{phone_to_delete}` have been successfully removed."
    except Exception as e:
        response['message'] = f"❌ An error occurred during the logout process: {e}"
        print(f"[{phone_to_delete}] ERROR during logout: {e}")
    return response

@controller.on(events.NewMessage(pattern=re.compile(r"^/logout(\s+.+)?$")))
async def logout_function(event):
    if not event.is_private: return
    parts = event.raw_text.split()
    if len(parts) != 2: return await event.respond("Usage: `/logout <phone>`", parse_mode='md')
    phone = parts[1]
    user_info = user_data.get(phone)
    if not user_info or user_info.get('controller_id') != event.sender_id:
        return await event.respond("❌ You can only log out numbers you have registered yourself.")
    result = await perform_logout(phone)
    await event.respond(result['message'], parse_mode='md')

@controller.on(events.NewMessage(pattern=re.compile(r"^/start$")))
async def start_function(event):
    if not event.is_private: return
    user_phones = [p for p, d in user_data.items() if d.get('controller_id') == event.sender_id]
    
    if user_phones:
        phone = user_phones[0]
        client = clients.get(phone)
        is_active = client and client.is_connected()

        if is_active:
            sender = await event.get_sender()
            name = sender.first_name
            user_id = event.sender_id
            phone_list = "\n".join(f"  - `{p}`" for p in user_phones)
            message = (
                f"👋 **Welcome back!**\n\n"
                f"👤 **Name:** {name}\n"
                f"🆔 **User ID:** `{user_id}`\n"
                f"📱 **Logged in with:**\n{phone_list}\n\n"
                "Use /help for commands."
            )
            await event.respond(message, parse_mode='md')
        else:
            sender = await event.get_sender()
            relogin_message = (
                f"**⚠️ Session Inactive**\n\n"
                f"Hello **{sender.first_name}**! It looks like you're registered with **`{phone}`**, but your session is no longer active.\n\n"
                "Please log in again to restart the service. Your saved settings (sources and targets) will be preserved.\n\n"
                "**Available Login Methods:**\n"
                "➡️ `/session_string <phone> <string>`\n"
                "**Generate Session String 👇👉 t.me/Auto_Forwarding_Post_bot/login**\n\n"
                "➡️ `/login <phone>`\n"
                "➡️ `/loginotp <phone>`\n"
                "➡️ `/session <phone>`"
            )
            await event.respond(relogin_message, parse_mode='md')
    else:
        # This is a brand new user
        login_guide = (
            "**You are Not Logged in.**\n"
            "Please log in first to use the bot.\n\n"
            "**📱TELEGRAM CONNECTION GUIDE📱**\n\n"
            "**Available Login Methods:**\n\n"
            "➡️ /session_string <phone> <string> - **Login via session string.**\n\n"
            "**👇अपने Session string को जनरेट करें👇**\n"
            "Generate To Bot 👉 t.me/Auto_Forwarding_Post_bot/login\n"
            "Generate To Web 👉 bit.ly/LoginToBot_Web\n\n"
            "➡️ /login <phone> - **Get a QR code to scan.**\n\n"
            "➡️ /loginotp <phone> - **Receive a login code in your Telegram app.**\n\n"
            "➡️ /session <phone> - **Upload a Telethon session file.**"
        )
        await event.respond(login_guide, parse_mode='md')

@controller.on(events.NewMessage(pattern=re.compile(r"^/help$")))
async def help_function(event):
    if not event.is_private: return
    txt = (
        "🛠 **Bot Guide**\n\n"
        "**Login Commands:**\n"
        "  `/login <phone>` – Login via QR code.\n"
        "  `/loginotp <phone>` – Login via OTP code.\n"
        "  `/session <phone>` – Login by uploading a session file.\n"
        "  `/session_string <phone> <string>` – Login via session string.\n"
        "\n**Login Steps:**\n"
        "  `/otp <code>` – Submit the OTP code.\n"
        "  `/password <pass>` – Submit your 2FA password.\n"
        "\n**Account Management:**\n"
        "  `/logout <phone>` – Log out and delete an account's data.\n"
        "\n**Forwarding Commands:**\n"
        "  `/add_source <id>` – Add a source chat.\n"
        "  `/add_target <id>` – Add a target chat.\n"
        "  `/remove_source <id>` – Remove a source chat.\n"
        "  `/remove_target <id>` – Remove a target chat.\n"
        "  `/list_source` – Show sources.\n"
        "  `/list_target` – Show targets.\n"
        "  `/getgroups` – List all your chats.\n"
        "  `/restart_forwarding` – Resume forwarding.\n"
        "  `/stop_forwarding` – Pause forwarding.\n"
        "  `/auto_message <min|stop>` – Manage auto-messaging.\n"
        "  `/delaytime <min> <max>` – Set custom delay between forwards."
    )
    await event.respond(txt, parse_mode='md')

async def heartbeat():
    while True:
        print("... Bot is alive and listening for events ...")
        await asyncio.sleep(60)

async def main():
    start_flask_thread()
    asyncio.create_task(heartbeat())
    for phone in list(user_data.keys()):
        if os.path.exists(f"{phone}.session"):
            asyncio.create_task(start_or_resume_client(phone))
    await controller.start(bot_token=BOT_TOKEN)
    bot_info = await controller.get_me()
    print(f"🚀 Controller bot @{bot_info.username} is ready.")
    await controller.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate images using Slash Commands.
Supports per-server configuration, scope, check mode, time limits,
history scanning (/scan), hash management, user allowlisting,
retroactive flag clearing (/clearflags), configurable replies (with on/off switch),
logging to a designated channel, and catching up on missed messages on startup.
Loads token from .env, settings from server_configs.json.
Requires discord.py v2.x (Pycord)
"""

import discord
# Note: We use discord.Bot which includes application command support directly
# from discord.ext import commands # No longer needed for prefix commands
import os
import sys
import json
import asyncio
import io
from PIL import Image, UnidentifiedImageError
import imagehash
from functools import partial
from dotenv import load_dotenv
import traceback
import typing
import datetime # For timestamps
import dateutil.parser # For parsing ISO timestamps easily
import re # For parsing message links
from collections import defaultdict # For grouping hashes during scan

# --- Constants ---
CONFIG_FILE_PATH = 'server_configs.json'
LAST_SEEN_FILE_PATH = 'last_seen.json' # File to store last startup timestamp
HASH_FILENAME_PREFIX = "hashes_"
VALID_SCOPES = ["server", "channel"]
VALID_CHECK_MODES = ["strict", "owner_allowed"]
DEFAULT_SCAN_LIMIT = 1000
DEFAULT_CATCHUP_LIMIT = 100 # Default limit for catch-up per channel
SCAN_UPDATE_INTERVAL = 100 # How often to update status message during scan
SCAN_ACTION_DELAY = 0.35 # Delay between actions on duplicates during scan
CLEAR_REACTION_DELAY = 0.2 # Delay between clearing reactions
CATCHUP_PROCESS_DELAY = 0.1 # Small delay between processing messages in catch-up

# Default Reply Template Placeholders:
# {mention}: Mention of the user who posted the duplicate.
# {filename}: Original filename of the duplicate image.
# {identifier}: The internal identifier for the matched original image (message_id-filename).
# {distance}: The hash distance (similarity score) between the images.
# {original_user_mention}: Mention of the user who posted the original image (if known).
# {emoji}: The currently configured `duplicate_reaction_emoji`.
# {original_user_info}: Expands to ", Orig User: <@user_id>" if the original user is known, otherwise empty string.
# {jump_link}: Expands to "\nOriginal: <message_link>" if the original message ID is known, otherwise empty string.
DEFAULT_REPLY_TEMPLATE = "{emoji} Hold on, {mention}! Image `{filename}` similar to recent submission (ID: `{identifier}`, Dist: {distance}{original_user_info}).{jump_link}"


# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# --- Global Configuration Cache & Locks ---
server_configs = {}
config_lock = asyncio.Lock()
hash_file_locks = {}
# Global dictionary to hold loaded guild hashes during catchup/scan to avoid repeated loads
# Key: guild_id, Value: loaded_hashes_dict
# This is cleared after catchup/scan finishes for a guild
active_hash_databases = {}

# --- Timestamp Persistence ---
def load_last_seen_timestamp() -> datetime.datetime | None:
    """Loads the last seen UTC timestamp from the JSON file."""
    try:
        if os.path.exists(LAST_SEEN_FILE_PATH):
            with open(LAST_SEEN_FILE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                timestamp_str = data.get('last_startup_utc')
                if timestamp_str:
                    dt = dateutil.parser.isoparse(timestamp_str)
                    # Ensure it's timezone-aware (UTC)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    print(f"INFO: Loaded last seen timestamp: {dt.isoformat()}")
                    return dt
    except json.JSONDecodeError:
        print(f"Warning: Could not decode JSON from '{LAST_SEEN_FILE_PATH}'. Assuming first run.")
    except Exception as e:
        print(f"Warning: Could not load last seen timestamp from '{LAST_SEEN_FILE_PATH}': {e}")
    return None

def save_current_timestamp(timestamp_utc: datetime.datetime):
    """Saves the current UTC timestamp to the JSON file."""
    try:
        # Ensure timezone info is present
        if timestamp_utc.tzinfo is None:
            timestamp_utc = timestamp_utc.replace(tzinfo=datetime.timezone.utc)

        with open(LAST_SEEN_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'last_startup_utc': timestamp_utc.isoformat()}, f, indent=4)
        # print(f"DEBUG: Saved current startup timestamp: {timestamp_utc.isoformat()}") # Can be noisy
    except Exception as e:
        print(f"Error: Could not save current timestamp to '{LAST_SEEN_FILE_PATH}': {e}")


# --- Configuration Loading/Saving ---

def get_default_guild_config(guild_id):
    """Returns default settings, including catch-up options."""
    return {
        "hash_db_file": f"{HASH_FILENAME_PREFIX}{guild_id}.json",
        "hash_size": 8,
        "similarity_threshold": 5,
        "allowed_channel_ids": None, # Explicitly list channels to monitor, None means monitor all
        "react_to_duplicates": True,
        "delete_duplicates": False,
        "reply_on_duplicate": True, # Controls replies for NEW posts
        "duplicate_reaction_emoji": "⚠️",
        "duplicate_scope": "server",
        "duplicate_check_mode": "strict",
        "duplicate_check_duration_days": 0, # 0 = check forever
        "allowed_users": [], # List of user IDs exempt from checks
        "duplicate_reply_template": DEFAULT_REPLY_TEMPLATE,
        "log_channel_id": None, # Channel ID for logging events
        "enable_catchup_on_startup": False, # New: Master switch for catch-up
        "catchup_limit_per_channel": DEFAULT_CATCHUP_LIMIT # New: Limit for catch-up
    }

def validate_config_data(config_data):
    """Validates config, including new catch-up fields."""
    validated = get_default_guild_config(0).copy() # Start with defaults
    validated.update(config_data) # Update with provided data
    try:
        # Coerce types
        validated['hash_size'] = int(validated['hash_size'])
        validated['similarity_threshold'] = int(validated['similarity_threshold'])
        validated['react_to_duplicates'] = bool(validated['react_to_duplicates'])
        validated['delete_duplicates'] = bool(validated['delete_duplicates'])
        validated['reply_on_duplicate'] = bool(validated.get('reply_on_duplicate', True))
        validated['duplicate_check_duration_days'] = int(validated.get('duplicate_check_duration_days', 0))
        if validated['duplicate_check_duration_days'] < 0: validated['duplicate_check_duration_days'] = 0

        # Catch-up Settings Validation
        validated['enable_catchup_on_startup'] = bool(validated.get('enable_catchup_on_startup', False))
        validated['catchup_limit_per_channel'] = int(validated.get('catchup_limit_per_channel', DEFAULT_CATCHUP_LIMIT))
        if validated['catchup_limit_per_channel'] <= 0: validated['catchup_limit_per_channel'] = DEFAULT_CATCHUP_LIMIT

        # Validate enums
        if validated.get('duplicate_scope') not in VALID_SCOPES: validated['duplicate_scope'] = "server"
        if validated.get('duplicate_check_mode') not in VALID_CHECK_MODES: validated['duplicate_check_mode'] = "strict"

        # Validate allowed_channel_ids (list of ints or None)
        if validated['allowed_channel_ids'] is not None:
            if isinstance(validated['allowed_channel_ids'], list):
                # Ensure all elements are digits before converting
                validated['allowed_channel_ids'] = [int(ch_id) for ch_id in validated['allowed_channel_ids'] if isinstance(ch_id, (int, str)) and str(ch_id).isdigit()]
                # If the list becomes empty after validation, set to None (consistent with default)
                if not validated['allowed_channel_ids']: validated['allowed_channel_ids'] = None
            else: # If it's not a list, reset to None
                validated['allowed_channel_ids'] = None

        # Validate allowed_users (list of ints or empty list)
        if 'allowed_users' not in validated or not isinstance(validated['allowed_users'], list):
            validated['allowed_users'] = []
        else:
            validated['allowed_users'] = [int(u_id) for u_id in validated['allowed_users'] if isinstance(u_id, (int, str)) and str(u_id).isdigit()]

        # Ensure reply template is a string
        if not isinstance(validated.get('duplicate_reply_template'), str):
            validated['duplicate_reply_template'] = DEFAULT_REPLY_TEMPLATE

        # Validate log_channel_id (must be int or None)
        log_id = validated.get('log_channel_id')
        if log_id is not None:
            try: validated['log_channel_id'] = int(log_id)
            except (ValueError, TypeError): validated['log_channel_id'] = None

    except (ValueError, TypeError, KeyError) as e:
        print(f"Warning: Error validating config types/keys: {e}. Some defaults may be used.", file=sys.stderr)
    return validated

async def load_main_config():
    """Loads the main server_configs.json file."""
    global server_configs
    async with config_lock:
        try:
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
            if not isinstance(loaded_data, dict): raise TypeError("Config is not a dictionary")
            validated_configs = {}
            for guild_id_str, guild_config_data in loaded_data.items():
                try:
                    guild_id = int(guild_id_str)
                    validated_configs[guild_id] = validate_config_data(guild_config_data)
                    validated_configs[guild_id]['hash_db_file'] = f"{HASH_FILENAME_PREFIX}{guild_id}.json"
                except ValueError: print(f"Warning: Invalid guild ID '{guild_id_str}' in config file. Skipping.", file=sys.stderr)
            server_configs = validated_configs
            print(f"Successfully loaded configurations for {len(server_configs)} guilds.")
        except FileNotFoundError: print(f"Info: Config file '{CONFIG_FILE_PATH}' not found. Defaults will be used."); server_configs = {}
        except json.JSONDecodeError as e: print(f"Error: Could not decode JSON from '{CONFIG_FILE_PATH}': {e}"); server_configs = {}
        except Exception as e: print(f"Error loading main config: {e}"); server_configs = {}

async def save_main_config():
    """Saves the global server_configs cache."""
    async with config_lock:
        config_to_save = {str(gid): data for gid, data in server_configs.items()}
        try:
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f: json.dump(config_to_save, f, indent=4)
            return True
        except Exception as e: print(f"DEBUG: Error saving main config: {e}"); return False

def get_guild_config(guild_id):
    """Gets guild config, ensures defaults exist (incl. new fields)."""
    global server_configs
    defaults_needed = False
    if guild_id not in server_configs:
        print(f"DEBUG: Creating default config for new guild ID: {guild_id}")
        server_configs[guild_id] = get_default_guild_config(guild_id)
        defaults_needed = True
    else:
        guild_conf = server_configs[guild_id]
        default_conf = get_default_guild_config(guild_id)
        updated = False
        for key, default_value in default_conf.items():
            if key not in guild_conf:
                print(f"DEBUG: Adding missing key '{key}' with default value to config for guild {guild_id}")
                guild_conf[key] = default_value; updated = True
        if updated:
            print(f"DEBUG: Re-validating config for guild {guild_id} after adding missing keys.")
            server_configs[guild_id] = validate_config_data(guild_conf)
            defaults_needed = True

    if defaults_needed: asyncio.create_task(save_main_config())
    return server_configs[guild_id]

async def save_guild_config(guild_id, guild_config_data):
    """Updates guild config and saves main file."""
    global server_configs
    server_configs[guild_id] = validate_config_data(guild_config_data)
    server_configs[guild_id]['hash_db_file'] = f"{HASH_FILENAME_PREFIX}{guild_id}.json"
    return await save_main_config()


# --- Hashing and File I/O Functions ---

def get_hash_file_lock(guild_id):
    global hash_file_locks
    if guild_id not in hash_file_locks: hash_file_locks[guild_id] = asyncio.Lock()
    return hash_file_locks[guild_id]

def calculate_hash_sync(image_bytes, hash_size):
    try: img = Image.open(io.BytesIO(image_bytes)); return imagehash.dhash(img, hash_size=hash_size)
    except UnidentifiedImageError: return None
    except Exception: return None
async def calculate_hash(image_bytes, hash_size, loop):
    func = partial(calculate_hash_sync, image_bytes, hash_size); return await loop.run_in_executor(None, func)

def load_hashes_sync(db_file):
    if not os.path.exists(db_file): return {}
    try:
        with open(db_file, 'r', encoding='utf-8') as f: data = json.load(f)
        if not isinstance(data, dict): return {}
        is_new_format_likely = False; has_timestamp = False
        if data:
            first_val = next(iter(data.values()), None)
            if isinstance(first_val, dict) and 'hash' in first_val and 'user_id' in first_val:
                is_new_format_likely = True
                if 'timestamp' in first_val: has_timestamp = True
            elif isinstance(first_val, dict):
                nested_val = next(iter(first_val.values()), None)
                if isinstance(nested_val, dict) and 'hash' in nested_val and 'user_id' in nested_val:
                    is_new_format_likely = True
                    if 'timestamp' in nested_val: has_timestamp = True
        if is_new_format_likely and not has_timestamp and data: print(f"Warning: Hash file '{db_file}' missing timestamps.", file=sys.stderr)
        elif not is_new_format_likely and data: print(f"Warning: Hash file '{db_file}' seems old format.", file=sys.stderr)
        return data
    except json.JSONDecodeError as e: print(f"DEBUG: Error decoding JSON from hash db '{db_file}': {e}"); return {}
    except Exception as e: print(f"DEBUG: Error loading hash db '{db_file}': {e}"); return {}

def save_hashes_sync(hashes_dict, db_file):
    try:
        with open(db_file, 'w', encoding='utf-8') as f: json.dump(hashes_dict, f, indent=4)
        return True
    except Exception as e: print(f"DEBUG: Error saving hash db '{db_file}': {e}"); return False

async def load_guild_hashes(guild_id, loop):
    if guild_id in active_hash_databases:
        return active_hash_databases[guild_id]
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(load_hashes_sync, db_file); loaded_hashes = await loop.run_in_executor(None, func)
    active_hash_databases[guild_id] = loaded_hashes
    return loaded_hashes

async def save_guild_hashes(guild_id, hashes_dict, loop):
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(save_hashes_sync, hashes_dict, db_file); success = await loop.run_in_executor(None, func)
    if success:
        active_hash_databases[guild_id] = hashes_dict.copy()
    return success

# --- Duplicate Finding ---

def find_existing_hash_entry_sync(target_hash, stored_hashes_dict, threshold, scope, channel_id_str):
    if target_hash is None: return None, None
    hashes_to_check = {}
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return None, None
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        if isinstance(hash_data, dict) and 'hash' in hash_data: stored_hash_str = hash_data.get('hash')
        elif isinstance(hash_data, str): stored_hash_str = hash_data
        if stored_hash_str is None: continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            if (target_hash - stored_hash) <= threshold:
                return identifier, hash_data
        except ValueError: continue
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}' against '{target_hash}': {e}", file=sys.stderr)
    return None, None

def find_duplicates_sync(new_image_hash, stored_hashes_dict, threshold, scope, channel_id_str, check_duration_days):
    duplicates = []
    if new_image_hash is None: return duplicates
    hashes_to_check = {}
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return duplicates
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff_time = now - datetime.timedelta(days=check_duration_days) if check_duration_days > 0 else None
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None; original_user_id = None; timestamp_str = None
        if isinstance(hash_data, dict):
            stored_hash_str = hash_data.get('hash'); original_user_id = hash_data.get('user_id'); timestamp_str = hash_data.get('timestamp')
        elif isinstance(hash_data, str):
            stored_hash_str = hash_data
            if check_duration_days > 0: continue
        else: continue
        if stored_hash_str is None: continue
        if cutoff_time and timestamp_str:
            try:
                stored_time = dateutil.parser.isoparse(timestamp_str)
                if stored_time.tzinfo is None: stored_time = stored_time.replace(tzinfo=datetime.timezone.utc)
                if stored_time < cutoff_time: continue
            except Exception: continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = new_image_hash - stored_hash
            if distance <= threshold:
                original_message_id = None
                try: original_message_id = int(identifier.split('-')[0])
                except: pass
                duplicates.append({'identifier': identifier, 'distance': distance, 'original_message_id': original_message_id, 'original_user_id': original_user_id})
        except ValueError: continue
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}' against '{new_image_hash}': {e}", file=sys.stderr)
    duplicates.sort(key=lambda x: x['distance'])
    return duplicates

async def find_duplicates(new_image_hash, stored_hashes_dict, threshold, scope, channel_id, check_duration_days, loop):
    func = partial(find_duplicates_sync, new_image_hash, stored_hashes_dict, threshold, scope, str(channel_id), check_duration_days)
    duplicates = await loop.run_in_executor(None, func)
    return duplicates


# --- Logging Helper ---
async def log_event(guild: discord.Guild, embed: discord.Embed):
    if not guild: return
    guild_config = get_guild_config(guild.id)
    log_channel_id = guild_config.get('log_channel_id')
    if log_channel_id:
        try:
            log_channel = bot.get_channel(log_channel_id) or await bot.fetch_channel(log_channel_id)
            if isinstance(log_channel, discord.TextChannel):
                if log_channel.permissions_for(guild.me).send_messages and log_channel.permissions_for(guild.me).embed_links:
                    await log_channel.send(embed=embed)
                else: print(f"Warning: [LogEvent G:{guild.id}] Missing Send/Embed permission in log channel {log_channel_id}.")
            else: print(f"Warning: [LogEvent G:{guild.id}] Log channel {log_channel_id} is not a text channel.")
        except discord.NotFound: print(f"Warning: [LogEvent G:{guild.id}] Log channel {log_channel_id} not found.")
        except discord.Forbidden: print(f"Warning: [LogEvent G:{guild.id}] No permission for log channel {log_channel_id}.")
        except Exception as e: print(f"Error: [LogEvent G:{guild.id}] Failed log send to {log_channel_id}: {e}")


# --- Discord Bot Implementation ---
intents = discord.Intents.default(); intents.message_content = True; intents.guilds = True; intents.members = True; intents.reactions = True
bot = discord.Bot(intents=intents)

# --- Catch-up Processing Helper ---
async def process_catchup_message(message: discord.Message, guild_config: dict, loop: asyncio.AbstractEventLoop) -> bool:
    """Processes a single message during the catch-up phase."""
    guild_id = message.guild.id
    channel_id = message.channel.id
    channel_id_str = str(channel_id)
    current_scope = guild_config.get('duplicate_scope', 'server')
    current_hash_size = guild_config.get('hash_size', 8)
    db_updated = False

    stored_hashes = await load_guild_hashes(guild_id, loop) # Uses cache

    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith('image/'):
            try:
                image_bytes = await attachment.read()
                img_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                if img_hash is None: continue

                existing_identifier, existing_data = find_existing_hash_entry_sync(img_hash, stored_hashes, 0, current_scope, channel_id_str)
                message_timestamp = message.created_at.replace(tzinfo=datetime.timezone.utc)
                update_needed = False
                identifier_to_use = f"{message.id}-{attachment.filename}"
                data_to_use = {"hash": str(img_hash), "user_id": message.author.id, "timestamp": message_timestamp.isoformat()}

                if existing_identifier:
                    existing_timestamp_str = None
                    if isinstance(existing_data, dict): existing_timestamp_str = existing_data.get('timestamp')
                    if existing_timestamp_str:
                        try:
                            existing_time = dateutil.parser.isoparse(existing_timestamp_str)
                            if existing_time.tzinfo is None: existing_time = existing_time.replace(tzinfo=datetime.timezone.utc)
                            if message_timestamp < existing_time: update_needed = True
                        except Exception: update_needed = True
                    else: update_needed = True
                else: update_needed = True

                if update_needed:
                    db_updated = True
                    if current_scope == "server":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        if existing_identifier and existing_identifier != identifier_to_use: stored_hashes.pop(existing_identifier, None)
                        stored_hashes[identifier_to_use] = data_to_use
                    elif current_scope == "channel":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        channel_hashes = stored_hashes.setdefault(channel_id_str, {})
                        if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[channel_id_str] = channel_hashes
                        if existing_identifier and existing_identifier != identifier_to_use: channel_hashes.pop(existing_identifier, None)
                        channel_hashes[identifier_to_use] = data_to_use

            except discord.HTTPException as e: print(f"Warning [CatchUp G:{guild_id}]: HTTP error attach '{attachment.filename}' (Msg {message.id}): {e.status} {e.code}")
            except Exception as e: print(f"Error [CatchUp G:{guild_id}]: Unexpected error attach '{attachment.filename}' (Msg {message.id}): {e}"); traceback.print_exc()
    return db_updated


# --- Event Handlers ---
@bot.event
async def on_ready():
    """Called when the bot is ready and has connected to Discord."""
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---')

    previous_start_time = load_last_seen_timestamp()
    current_start_time = datetime.datetime.now(datetime.timezone.utc)
    save_current_timestamp(current_start_time)

    await load_main_config()
    print(f'--- Configs loaded. Bot sees {len(bot.guilds)} guilds. ---')
    for guild in bot.guilds: _ = get_guild_config(guild.id)

    try:
        print("--- Syncing slash commands... ---")
        await bot.sync_commands()
        print("--- Slash commands synced ---")
    except Exception as e: print(f"--- Failed to sync slash commands: {e} ---")

    # --- Catch-up Logic ---
    if previous_start_time:
        print(f"--- Starting catch-up process for messages since {previous_start_time.isoformat()} ---")
        catchup_start_time = datetime.datetime.now()
        guilds_processed = 0; channels_scanned = 0; messages_processed = 0
        guild_dbs_to_save = set()
        loop = asyncio.get_running_loop()

        for guild in bot.guilds:
            guild_id = guild.id
            guild_config = get_guild_config(guild_id)

            if not guild_config.get('enable_catchup_on_startup', False):
                continue # Skip guild if catch-up is disabled

            guilds_processed += 1
            guild_db_updated = False

            # Determine channels based on allowed_channel_ids
            allowed_channel_ids = guild_config.get('allowed_channel_ids')
            channels_to_scan = []
            scan_type_msg = ""

            if allowed_channel_ids: # If a list of specific channels is provided
                scan_type_msg = "configured channels"
                for ch_id in allowed_channel_ids:
                    channel = guild.get_channel(ch_id)
                    if channel and isinstance(channel, discord.TextChannel) and channel.permissions_for(guild.me).read_message_history:
                        channels_to_scan.append(channel)
                    elif channel: print(f"Warning [CatchUp G:{guild_id}]: Cannot scan configured channel '{channel.name}' ({ch_id}) due to type or permissions.")
                    else: print(f"Warning [CatchUp G:{guild_id}]: Configured channel ID {ch_id} not found in guild.")
            else: # If allowed_channel_ids is None or empty, scan all readable channels
                scan_type_msg = "all readable channels"
                channels_to_scan = [ch for ch in guild.text_channels if ch.permissions_for(guild.me).read_message_history]

            if not channels_to_scan:
                print(f"INFO [CatchUp G:{guild_id}]: No channels to scan for guild '{guild.name}'. Skipping.")
                continue

            print(f"INFO [CatchUp G:{guild_id}]: Catch-up enabled for guild '{guild.name}'. Checking {scan_type_msg}.")
            limit_per_channel = guild_config.get('catchup_limit_per_channel', DEFAULT_CATCHUP_LIMIT)

            for channel in channels_to_scan:
                channels_scanned += 1
                channel_messages_processed = 0
                print(f"DEBUG [CatchUp G:{guild_id}]: Scanning channel '{channel.name}' (Limit: {limit_per_channel})...")
                try:
                    async for message in channel.history(after=previous_start_time, limit=limit_per_channel, oldest_first=True):
                        if message.author.bot: continue
                        messages_processed += 1
                        channel_messages_processed += 1
                        if await process_catchup_message(message, guild_config, loop):
                            guild_db_updated = True
                        await asyncio.sleep(CATCHUP_PROCESS_DELAY)
                    # print(f"DEBUG [CatchUp G:{guild_id}]: Finished channel '{channel.name}', processed {channel_messages_processed} messages.") # Can be noisy
                except discord.Forbidden: print(f"Warning [CatchUp G:{guild_id}]: Permission error during history fetch for '{channel.name}'.")
                except Exception as e: print(f"Error [CatchUp G:{guild_id}]: Failed processing channel '{channel.name}': {e}"); traceback.print_exc()

            if guild_db_updated: guild_dbs_to_save.add(guild_id)

        if guild_dbs_to_save:
            print(f"INFO [CatchUp]: Saving updated hash databases for {len(guild_dbs_to_save)} guilds...")
            save_tasks = [save_guild_hashes(gid, active_hash_databases[gid], loop) for gid in guild_dbs_to_save if gid in active_hash_databases]
            results = await asyncio.gather(*save_tasks, return_exceptions=True)
            saved_count = sum(1 for r in results if r is True); failed_count = len(results) - saved_count
            print(f"INFO [CatchUp]: Hash DB Save Results - Success: {saved_count}, Failed: {failed_count}")
            if failed_count > 0: print(f"ERROR [CatchUp]: Failures occurred during hash DB saving: {results}")

        active_hash_databases.clear()
        print("DEBUG: Cleared active hash database cache.")
        catchup_elapsed = (datetime.datetime.now() - catchup_start_time).total_seconds()
        print(f"--- Catch-up process finished ({catchup_elapsed:.2f}s) ---")
        print(f"    Guilds Checked: {guilds_processed}, Channels Scanned: {channels_scanned}, Messages Processed: {messages_processed}")
    else:
        print("--- No previous start time found. Skipping catch-up process. ---")

    print('------ Bot is fully ready! ------')


@bot.event
async def on_guild_join(guild):
    print(f"Joined new guild: {guild.name} (ID: {guild.id})");
    _ = get_guild_config(guild.id);
    await save_main_config()

@bot.event
async def on_message(message):
    """Handles image processing for NEW messages (NOT commands or catch-up)."""
    if message.guild is None or message.author == bot.user or message.author.bot: return

    guild_id = message.guild.id; guild_config = get_guild_config(guild_id)
    current_user_id = message.author.id; allowed_users = guild_config.get('allowed_users', [])
    if current_user_id in allowed_users: return

    channel_id = message.channel.id; channel_id_str = str(channel_id)
    allowed_channel_ids = guild_config.get('allowed_channel_ids')
    # Regular operation checks all if None/empty
    if allowed_channel_ids is not None and channel_id not in allowed_channel_ids: return
    if not message.attachments: return

    loop = asyncio.get_running_loop()
    stored_hashes = await load_guild_hashes(guild_id, loop)
    db_updated = False

    current_scope = guild_config.get('duplicate_scope', 'server')
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    current_hash_size = guild_config.get('hash_size', 8)
    current_similarity_threshold = guild_config.get('similarity_threshold', 5)
    react_to_duplicates = guild_config.get('react_to_duplicates', True)
    delete_duplicates = guild_config.get('delete_duplicates', False)
    reply_on_duplicate = guild_config.get('reply_on_duplicate', True)
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE)

    for i, attachment in enumerate(message.attachments):
        if not (attachment.content_type and attachment.content_type.startswith('image/')): continue

        try:
            image_bytes = await attachment.read()
            new_hash = await calculate_hash(image_bytes, current_hash_size, loop)
            if new_hash is None: print(f"Debug: [G:{guild_id}] Could not hash attach {attachment.filename} msg {message.id}"); continue

            duplicate_matches = await find_duplicates(new_hash, stored_hashes, current_similarity_threshold, current_scope, channel_id, current_duration, loop)
            is_violation = False; violating_match = None
            if duplicate_matches:
                if current_mode == "strict": is_violation = True; violating_match = duplicate_matches[0]
                elif current_mode == "owner_allowed":
                    for match in duplicate_matches:
                        if match.get('original_user_id') is None or match.get('original_user_id') != current_user_id:
                            is_violation = True; violating_match = match; break

            if is_violation and violating_match:
                identifier = violating_match['identifier']; distance = violating_match['distance']
                original_message_id = violating_match.get('original_message_id'); original_user_id = violating_match.get('original_user_id')
                original_msg_link = None
                if reply_on_duplicate:
                    template_data = { "mention": message.author.mention, "filename": attachment.filename, "identifier": identifier, "distance": distance,
                                      "original_user_mention": f"<@{original_user_id}>" if original_user_id else "*Unknown*", "emoji": duplicate_reaction_emoji,
                                      "original_user_info": f", Orig User: <@{original_user_id}>" if original_user_id else "", "jump_link": "" }
                    if original_message_id and message.guild:
                        try: jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"; template_data["jump_link"] = f"\nOriginal: {jump_url}"; original_msg_link = jump_url
                        except Exception as e: print(f"DEBUG: Failed jump link build: {e}")
                    reply_text = reply_template.format_map(defaultdict(str, template_data))
                    try: await message.reply(reply_text, mention_author=True)
                    except discord.HTTPException as e: print(f"Error: Failed reply msg {message.id}: {e}")
                log_embed = discord.Embed(title="Duplicate Image Detected", color=discord.Color.orange(), timestamp=datetime.datetime.now(datetime.timezone.utc))
                log_embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
                log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                log_embed.add_field(name="Message", value=f"[Jump Link]({message.jump_url})", inline=True)
                log_embed.add_field(name="Image Hash", value=f"`{new_hash}`", inline=False)
                log_embed.add_field(name="Match Identifier", value=f"`{identifier}`", inline=True)
                log_embed.add_field(name="Hash Distance", value=str(distance), inline=True)
                orig_user_mention = f"<@{original_user_id}> (`{original_user_id}`)" if original_user_id else "Unknown"
                log_embed.add_field(name="Original User", value=orig_user_mention, inline=True)
                if not original_msg_link and original_message_id and message.guild:
                    try: original_msg_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                    except: pass
                if original_msg_link: log_embed.add_field(name="Original Message", value=f"[Approx. Link]({original_msg_link})", inline=False)
                if attachment.url: log_embed.set_thumbnail(url=attachment.url)
                log_embed.set_footer(text=f"Guild ID: {guild_id}")
                await log_event(message.guild, log_embed)
                if react_to_duplicates:
                    try: await message.add_reaction(duplicate_reaction_emoji)
                    except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed reaction msg {message.id}: {e}")
                if delete_duplicates:
                    if message.channel.permissions_for(message.guild.me).manage_messages:
                        try: await message.delete()
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed delete msg {message.id}: {e}")
                    else: print(f"DEBUG: [G:{guild_id}] Lacking 'Manage Messages' perm delete msg {message.id}.")
            elif not is_violation:
                existing_identifier, _ = find_existing_hash_entry_sync(new_hash, stored_hashes, 0, current_scope, channel_id_str)
                if not existing_identifier:
                    unique_identifier = f"{message.id}-{attachment.filename}"
                    current_utc_time = datetime.datetime.now(datetime.timezone.utc)
                    hash_data_to_store = {"hash": str(new_hash), "user_id": current_user_id, "timestamp": current_utc_time.isoformat()}
                    if current_scope == "server":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        stored_hashes[unique_identifier] = hash_data_to_store
                    elif current_scope == "channel":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        channel_hashes = stored_hashes.setdefault(channel_id_str, {})
                        if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[channel_id_str] = channel_hashes
                        channel_hashes[unique_identifier] = hash_data_to_store
                    db_updated = True
        except discord.HTTPException as e: print(f"Warning: [G:{guild_id}] HTTP error attach '{attachment.filename}' (Msg {message.id}): {e.status} {e.code}", file=sys.stderr)
        except Exception as e: print(f"Error: [G:{guild_id}] Unexpected error attach '{attachment.filename}' (Msg {message.id}): {e}", file=sys.stderr); traceback.print_exc()

    if db_updated:
        if not await save_guild_hashes(guild_id, stored_hashes, loop): print(f"ERROR: [G:{guild_id}] Failed save hash DB after msg {message.id}!")


# --- Slash Command Definitions ---

# Helper for permission checks
async def check_admin_permissions(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member): await interaction.response.send_message("❌ Server only.", ephemeral=True); return False
    if not interaction.user.guild_permissions.administrator: await interaction.response.send_message("❌ Admin permissions required.", ephemeral=True); return False
    return True

# --- Config Commands ---

@bot.slash_command(name="config_view", description="Shows the current bot configuration for this server.")
async def config_view(interaction: discord.Interaction):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    embed = discord.Embed(title=f"Bot Configuration for {interaction.guild.name}", color=discord.Color.blue())
    scope = guild_config.get('duplicate_scope', 'server'); mode = guild_config.get('duplicate_check_mode', 'strict')
    duration = guild_config.get('duplicate_check_duration_days', 0); duration_str = f"{duration} days" if duration > 0 else "Forever"
    log_channel_id = guild_config.get('log_channel_id'); log_channel_mention = f"<#{log_channel_id}>" if log_channel_id else "Not Set"
    catchup_enabled = guild_config.get('enable_catchup_on_startup', False)
    catchup_limit = guild_config.get('catchup_limit_per_channel', DEFAULT_CATCHUP_LIMIT)
    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True); embed.add_field(name="Check Mode", value=f"`{mode}`", inline=True); embed.add_field(name="Check Duration", value=f"`{duration_str}`", inline=True)
    embed.add_field(name="Log Channel", value=log_channel_mention, inline=True); embed.add_field(name="Catch-up Enabled", value=f"`{catchup_enabled}`", inline=True); embed.add_field(name="Catch-up Limit/Channel", value=f"`{catchup_limit}`", inline=True)
    other_settings = []
    for key, value in guild_config.items():
        if key in ['duplicate_scope', 'duplicate_check_mode', 'duplicate_check_duration_days', 'log_channel_id', 'hash_db_file', 'enable_catchup_on_startup', 'catchup_limit_per_channel']: continue
        display_value = value; key_title = key.replace('_', ' ').title()
        # Modified display for allowed_channel_ids
        if key == 'allowed_channel_ids':
            display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels (Catch-up uses All)"
        elif key == 'allowed_users': display_value = ', '.join(f'<@{u_id}>' for u_id in value) if value else "None"
        elif key == 'duplicate_reply_template': display_value = f"```\n{value}\n```" if value else "`Default`"
        elif isinstance(value, bool): display_value = "Enabled" if value else "Disabled"
        else: display_value = f"`{value}`"
        other_settings.append(f"**{key_title}**: {str(display_value)}")
    if other_settings: embed.add_field(name="Other Settings", value="\n".join(other_settings), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def _update_config(interaction: discord.Interaction, setting: str, new_value: typing.Any):
    """Helper to update and save config, sending response VIA FOLLOWUP."""
    # This function assumes interaction has already been deferred by the calling command.
    if not interaction.guild_id:
        # This case should ideally not be reached if commands defer properly.
        # If it is, sending a response might fail if one was already sent.
        print("ERROR: _update_config called without guild_id.")
        try:
            await interaction.response.send_message("❌ Internal error: Missing server context.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send("❌ Internal error: Missing server context.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    guild_config = get_guild_config(guild_id).copy()
    original_value = guild_config.get(setting)

    if str(original_value) == str(new_value):
        await interaction.followup.send(f"ℹ️ Setting '{setting}' is already set to `{new_value}`.", ephemeral=True)
        return

    guild_config[setting] = new_value
    if await save_guild_config(guild_id, guild_config):
        display_new = new_value
        display_orig = original_value
        if setting == 'duplicate_check_duration_days':
            display_orig = f"{original_value} days" if original_value is not None and original_value > 0 else "Forever"
            display_new = f"{new_value} days" if new_value > 0 else "Forever"
        elif setting == 'duplicate_reply_template':
            display_orig = f"```\n{original_value}\n```" if original_value else "`Default`"
            display_new = f"```\n{new_value}\n```"
        elif setting == 'log_channel_id':
            display_orig = f"<#{original_value}>" if original_value else "`None`"
            display_new = f"<#{new_value}>" if new_value else "`None`"
        elif isinstance(new_value, bool):
            display_orig = "`Enabled`" if bool(original_value) else "`Disabled`"
            display_new = "`Enabled`" if new_value else "`Disabled`"
        else:
            display_orig = f"`{original_value}`"
            display_new = f"`{new_value}`"

        await interaction.followup.send(f"✅ Updated '{setting}' from {display_orig} to {display_new}.", ephemeral=True)
        if setting == 'duplicate_scope':
            await interaction.followup.send(f"⚠️ **Warning:** Changing scope might affect hash lookup.", ephemeral=True)
    else:
        await interaction.followup.send(f"⚠️ Failed to save config update for '{setting}'.", ephemeral=True)

@bot.slash_command(name="config_set_threshold", description="Sets similarity threshold (0-20, lower is stricter).")
async def config_set_threshold(interaction: discord.Interaction, value: discord.Option(int, "Threshold (0=exact match).", min_value=0, max_value=20)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "similarity_threshold", value)

@bot.slash_command(name="config_set_hash_size", description="Sets hash detail level (e.g., 8, 16). Higher needs more CPU.")
async def config_set_hash_size(interaction: discord.Interaction, value: discord.Option(int, "Hash size (power of 2, >= 4).", min_value=4, max_value=32)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    if not (value & (value - 1) == 0) and value != 0:
        await interaction.followup.send("⚠️ Warning: Hash size non-standard (use 4, 8, 16, 32). Setting anyway...", ephemeral=True)
    await _update_config(interaction, "hash_size", value)

@bot.slash_command(name="config_set_react", description="Enable/disable reacting to new duplicates.")
async def config_set_react(interaction: discord.Interaction, value: discord.Option(bool, "Enable reactions?")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "react_to_duplicates", value)

@bot.slash_command(name="config_set_delete", description="Enable/disable deleting new duplicates.")
async def config_set_delete(interaction: discord.Interaction, value: discord.Option(bool, "Enable deletion?")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "delete_duplicates", value)

@bot.slash_command(name="config_set_reply", description="Enable/disable replying to new duplicates.")
async def config_set_reply(interaction: discord.Interaction, value: discord.Option(bool, "Enable replies?")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "reply_on_duplicate", value)

@bot.slash_command(name="config_set_emoji", description="Sets the emoji for duplicate reactions.")
async def config_set_emoji(interaction: discord.Interaction, value: discord.Option(str, "The new emoji.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not interaction.guild: await interaction.response.send_message("❌ Guild context unavailable.", ephemeral=True); return # Should not happen

    await interaction.response.defer(ephemeral=True)

    test_message_object = None
    emoji_is_valid = False

    if interaction.channel and \
            interaction.channel.permissions_for(interaction.guild.me).send_messages and \
            interaction.channel.permissions_for(interaction.guild.me).add_reactions and \
            interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        try:
            test_message_object = await interaction.channel.send(f"Testing emoji: {value}")
            await test_message_object.add_reaction(value)
            await asyncio.sleep(0.5) # Brief pause
            await test_message_object.delete()
            emoji_is_valid = True
        except discord.HTTPException as e:
            error_message = f"❌ Invalid/inaccessible emoji, or bot lacks perms to react/delete test messages. Error: {e.text}"
            if test_message_object:
                try: await test_message_object.delete()
                except: pass
            await interaction.followup.send(content=error_message, ephemeral=True)
            return
        except Exception as e:
            error_message = f"❌ Error testing emoji: {e}"
            if test_message_object:
                try: await test_message_object.delete()
                except: pass
            await interaction.followup.send(content=error_message, ephemeral=True)
            return
    else:
        await interaction.followup.send("⚠️ Could not perform live emoji test (missing perms/channel context). Setting config directly.", ephemeral=True)
        emoji_is_valid = True # Assume valid

    if emoji_is_valid:
        await _update_config(interaction, "duplicate_reaction_emoji", value)


@bot.slash_command(name="config_set_scope", description="Sets duplicate check scope (server or channel).")
async def config_set_scope(interaction: discord.Interaction, value: discord.Option(str, "Choose scope.", choices=VALID_SCOPES)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "duplicate_scope", value)

@bot.slash_command(name="config_set_check_mode", description="Sets duplicate check mode (strict or owner_allowed).")
async def config_set_check_mode(interaction: discord.Interaction, value: discord.Option(str, "Choose check mode.", choices=VALID_CHECK_MODES)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "duplicate_check_mode", value)

@bot.slash_command(name="config_set_duration", description="Sets how many days back to check for duplicates (0=forever).")
async def config_set_duration(interaction: discord.Interaction, value: discord.Option(int, "Number of days (0 for forever).", min_value=0)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "duplicate_check_duration_days", value)

@bot.slash_command(name="config_set_log_channel", description="Sets or clears the channel for logging duplicates.")
async def config_set_log_channel(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "Log channel, or omit to disable.", required=False, default=None)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild context.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    new_channel_id = channel.id if channel else None
    if new_channel_id:
        log_channel_obj = interaction.guild.get_channel(new_channel_id) # Use guild.get_channel
        if not log_channel_obj or not isinstance(log_channel_obj, discord.TextChannel):
            await interaction.followup.send("❌ Invalid channel specified.", ephemeral=True); return
        bot_member = interaction.guild.me
        if not log_channel_obj.permissions_for(bot_member).send_messages or \
                not log_channel_obj.permissions_for(bot_member).embed_links:
            await interaction.followup.send(f"❌ Bot lacks Send/Embed perms in {channel.mention}.", ephemeral=True); return
        try:
            await log_channel_obj.send(embed=discord.Embed(description=f"✅ Log channel set by {interaction.user.mention}.", color=discord.Color.green()))
        except Exception as e:
            print(f"Warning: [G:{interaction.guild_id}] Failed confirmation send to log channel {new_channel_id}: {e}")

    await _update_config(interaction, "log_channel_id", new_channel_id)


@bot.slash_command(name="config_set_reply_template", description="Sets the template for duplicate reply messages.")
async def config_set_reply_template(interaction: discord.Interaction, template: discord.Option(str, "Template string. Use {placeholders}. Max 1500 chars.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    if not isinstance(template, str): await interaction.followup.send("❌ Template must be string.", ephemeral=True); return
    if len(template) > 1500: await interaction.followup.send("❌ Template too long (max 1500 chars).", ephemeral=True); return
    if template.count('{') != template.count('}'):
        # Send warning but still proceed to update
        await interaction.followup.send("⚠️ Warning: Template has unbalanced curly braces `{}`. Setting anyway.", ephemeral=True)

    # _update_config will send the primary confirmation
    await _update_config(interaction, "duplicate_reply_template", template)
    # Send placeholder info as a separate followup, as _update_config already sent one.
    await interaction.followup.send(f"ℹ️ Placeholders: `{{mention}}`, `{{filename}}`, `{{identifier}}`, `{{distance}}`, `{{original_user_mention}}`, `{{emoji}}`, `{{original_user_info}}`, `{{jump_link}}`", ephemeral=True)

# --- Catch-up Config Commands ---

@bot.slash_command(name="config_set_catchup_enabled", description="Enable/disable checking missed messages on bot startup.")
async def config_set_catchup_enabled(interaction: discord.Interaction, value: discord.Option(bool, "Enable catch-up?")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "enable_catchup_on_startup", value)

@bot.slash_command(name="config_set_catchup_limit", description="Sets max messages to check per channel during startup catch-up.")
async def config_set_catchup_limit(interaction: discord.Interaction, value: discord.Option(int, "Max messages per channel (e.g., 100).", min_value=10, max_value=1000)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True)
    await _update_config(interaction, "catchup_limit_per_channel", value)


# --- Config Channel Commands ---

@bot.slash_command(name="config_channel_view", description="Shows channels monitored (used for new posts AND catch-up).")
async def config_channel_view(interaction: discord.Interaction):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id); channel_list = guild_config.get('allowed_channel_ids')
    if channel_list: await interaction.response.send_message(embed=discord.Embed(title=f"Monitored Channels for {interaction.guild.name}", description='\n'.join(f"- <#{ch_id}> (`{ch_id}`)" for ch_id in channel_list), color=discord.Color.blue()), ephemeral=True)
    else: await interaction.response.send_message("ℹ️ Monitoring all channels for new posts (Catch-up also uses all channels).", ephemeral=True)

@bot.slash_command(name="config_channel_add", description="Adds a channel to monitor.")
async def config_channel_add(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "Channel to monitor.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True) # Defer for _update_config
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    allowed_channels = guild_config.get('allowed_channel_ids', [])
    if allowed_channels is None: allowed_channels = []
    if channel_id not in allowed_channels:
        allowed_channels.append(channel_id)
        guild_config['allowed_channel_ids'] = allowed_channels
        # Use _update_config to handle the response after deferring
        await _update_config(interaction, "allowed_channel_ids", allowed_channels)
    else: await interaction.followup.send(f"ℹ️ {channel.mention} already monitored.", ephemeral=True)


@bot.slash_command(name="config_channel_remove", description="Removes a channel from monitoring.")
async def config_channel_remove(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "Channel to stop monitoring.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True) # Defer for _update_config
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    allowed_channels = guild_config.get('allowed_channel_ids')
    if allowed_channels and channel_id in allowed_channels:
        allowed_channels.remove(channel_id);
        new_value = allowed_channels if allowed_channels else None
        # Use _update_config to handle the response after deferring
        await _update_config(interaction, "allowed_channel_ids", new_value)
        # Followup with additional info if needed
        if new_value is None:
            await interaction.followup.send("ℹ️ Now monitoring all channels.", ephemeral=True)
    elif allowed_channels is None: await interaction.followup.send(f"ℹ️ Currently monitoring all channels. Cannot remove {channel.mention}.", ephemeral=True)
    else: await interaction.followup.send(f"ℹ️ {channel.mention} not in monitored list.", ephemeral=True)

@bot.slash_command(name="config_channel_clear", description="Clears monitored channels (monitors all for new posts & catch-up).")
async def config_channel_clear(interaction: discord.Interaction):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True) # Defer for _update_config
    # Use _update_config to handle the response after deferring
    await _update_config(interaction, "allowed_channel_ids", None)


# --- Allowlist Commands ---

@bot.slash_command(name="allowlist_view", description="Shows the current user allowlist.")
async def allowlist_view(interaction: discord.Interaction):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id); user_list = guild_config.get('allowed_users', [])
    if user_list:
        embed = discord.Embed(title=f"Allowlisted Users for {interaction.guild.name}", color=discord.Color.green()); mentions = []
        for user_id in user_list:
            try: user = bot.get_user(user_id) or await bot.fetch_user(user_id); mentions.append(f"- {user.mention if user else '*Unknown*'} (`{user_id}`)")
            except discord.NotFound: mentions.append(f"- *Unknown (ID {user_id})*")
            except Exception as e: mentions.append(f"- *Error ({user_id})*") ; print(f"Error fetching user {user_id}: {e}")
        embed.description = '\n'.join(mentions); await interaction.response.send_message(embed=embed, ephemeral=True)
    else: await interaction.response.send_message("ℹ️ No users allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_add", description="Adds a user to the allowlist (exempt from checks).")
async def allowlist_add(interaction: discord.Interaction, user: discord.Option(discord.User, "User to allowlist.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True) # Defer for _update_config
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    allowed_users = guild_config.get('allowed_users', [])
    if allowed_users is None: allowed_users = []
    if user_id not in allowed_users:
        allowed_users.append(user_id)
        # Use _update_config to handle the response after deferring
        await _update_config(interaction, "allowed_users", allowed_users)
    else: await interaction.followup.send(f"ℹ️ {user.mention} already allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_remove", description="Removes a user from the allowlist.")
async def allowlist_remove(interaction: discord.Interaction, user: discord.Option(discord.User, "User to remove.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await interaction.response.defer(ephemeral=True) # Defer for _update_config
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    allowed_users = guild_config.get('allowed_users')
    if allowed_users and user_id in allowed_users:
        allowed_users.remove(user_id)
        # Use _update_config to handle the response after deferring
        await _update_config(interaction, "allowed_users", allowed_users)
    else: await interaction.followup.send(f"ℹ️ {user.mention} not allowlisted.", ephemeral=True)


# --- Hash Management Commands ---

def parse_message_id(message_ref: str) -> int | None:
    """Extracts message ID from link or plain ID. Corrected Syntax."""
    match = re.search(r'/(\d+)$', message_ref.strip())
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    elif message_ref.strip().isdigit():
        try:
            return int(message_ref.strip())
        except ValueError:
            return None
    return None

@bot.slash_command(name="hash_remove", description="Removes stored hash(es) for a specific message ID/link.")
async def remove_hash(interaction: discord.Interaction, message_reference: discord.Option(str, "Message ID or link.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()
    target_message_id = parse_message_id(message_reference)
    if target_message_id is None: await interaction.response.send_message("❌ Invalid message ID/link format.", ephemeral=True); return
    target_message_id_str = str(target_message_id)
    stored_hashes = await load_guild_hashes(guild_id, loop); stored_hashes = stored_hashes.copy()
    hash_removed = False; keys_removed = []
    target_channel_key = None
    if not isinstance(stored_hashes, dict): await interaction.response.send_message("ℹ️ Hash DB empty/invalid.", ephemeral=True); return
    if current_scope == "server":
        keys_to_remove_now = [k for k in stored_hashes if k.startswith(target_message_id_str + "-")]
        for key in keys_to_remove_now: del stored_hashes[key]; keys_removed.append(key); hash_removed = True
    elif current_scope == "channel":
        for ch_id_str, channel_hashes in stored_hashes.items():
            if isinstance(channel_hashes, dict):
                keys_to_remove_now = [k for k in channel_hashes if k.startswith(target_message_id_str + "-")]
                if keys_to_remove_now: target_channel_key = ch_id_str
                for key in keys_to_remove_now: del channel_hashes[key]; keys_removed.append(key); hash_removed = True
        if target_channel_key and not stored_hashes[target_channel_key]: del stored_hashes[target_channel_key]
    if hash_removed:
        if await save_guild_hashes(guild_id, stored_hashes, loop): await interaction.response.send_message(f"✅ Removed {len(keys_removed)} hash(es) for msg ID `{target_message_id}`.", ephemeral=True)
        else: await interaction.response.send_message("⚠️ Error saving hash DB.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ No hashes found for msg ID `{target_message_id}`.", ephemeral=True)

@bot.slash_command(name="hash_clear", description="Clears hashes for server or channel. Requires confirmation!")
async def clear_hashes(interaction: discord.Interaction, confirm: discord.Option(bool, "Confirm deletion?"), channel: discord.Option(discord.TextChannel, "Optional: Clear only this channel (if scope='channel').", required=False, default=None)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 Set `confirm:True` to proceed.", ephemeral=True); return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()
    target_desc = f"channel {channel.mention}" if channel else "the entire server"
    await interaction.response.defer(ephemeral=True)
    stored_hashes = await load_guild_hashes(guild_id, loop); stored_hashes = stored_hashes.copy()
    cleared = False; original_count = 0
    if not isinstance(stored_hashes, dict): await interaction.followup.send(f"ℹ️ Hash DB empty/invalid.", ephemeral=True); return
    if channel:
        target_channel_id_str = str(channel.id)
        if current_scope == "channel":
            if target_channel_id_str in stored_hashes: original_count = len(stored_hashes[target_channel_id_str]); del stored_hashes[target_channel_id_str]; cleared = True
            else: await interaction.followup.send(f"ℹ️ No hashes for {channel.mention} (Scope='channel').", ephemeral=True); return
        else: await interaction.followup.send(f"ℹ️ Cannot clear specific channel (Scope='server').", ephemeral=True); return
    else:
        if current_scope == "server": original_count = len(stored_hashes)
        elif current_scope == "channel": original_count = sum(len(v) for v in stored_hashes.values() if isinstance(v, dict))
        stored_hashes.clear(); cleared = True
    if cleared:
        if await save_guild_hashes(guild_id, stored_hashes, loop): await interaction.followup.send(f"✅ Cleared {original_count} hash(es) for {target_desc}.", ephemeral=True)
        else: await interaction.followup.send(f"⚠️ Error saving cleared hash DB.", ephemeral=True)


# --- Scan Command ---
@bot.slash_command(name="scan", description="Scans channel history to add/update image hashes.")
async def scan_history(
        interaction: discord.Interaction,
        channel: discord.Option(discord.TextChannel, "Channel to scan."),
        limit: discord.Option(int, "Max messages.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT),
        flag_duplicates: discord.Option(bool, "React to duplicates?", default=False),
        reply_to_duplicates: discord.Option(bool, "Reply to duplicates?", default=False),
        delete_duplicates: discord.Option(bool, "Delete duplicates? (Needs Manage Messages)", default=False),
        log_scan_duplicates: discord.Option(bool, "Log duplicates found?", default=False)
):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild context.", ephemeral=True); return
    guild_id = interaction.guild_id; scan_channel_id = channel.id; scan_channel_id_str = str(scan_channel_id)
    guild_config = get_guild_config(guild_id); current_scope = guild_config.get('duplicate_scope', 'server')
    current_hash_size = guild_config.get('hash_size', 8); existence_threshold = 0
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE)
    reply_on_scan_duplicate_config = guild_config.get('reply_on_duplicate', True)
    log_channel_id = guild_config.get('log_channel_id')
    bot_member = interaction.guild.me
    if not channel.permissions_for(bot_member).read_message_history: await interaction.response.send_message(f"❌ Bot lacks 'Read History' in {channel.mention}.", ephemeral=True); return
    perms_needed = []
    if flag_duplicates and not channel.permissions_for(bot_member).add_reactions: perms_needed.append("Add Reactions")
    if reply_on_scan_duplicate_config and reply_to_duplicates and not channel.permissions_for(bot_member).send_messages: perms_needed.append("Send Messages")
    if delete_duplicates and not channel.permissions_for(bot_member).manage_messages: perms_needed.append("Manage Messages")
    if log_scan_duplicates and not log_channel_id: await interaction.response.send_message(f"❌ Logging requested, but no log channel configured.", ephemeral=True); return
    if log_scan_duplicates and log_channel_id:
        log_channel_obj = interaction.guild.get_channel(log_channel_id) # Renamed to avoid conflict
        if not log_channel_obj or not isinstance(log_channel_obj, discord.TextChannel): perms_needed.append(f"Access Log Channel ({log_channel_id})")
        elif not log_channel_obj.permissions_for(bot_member).send_messages or not log_channel_obj.permissions_for(bot_member).embed_links: perms_needed.append(f"Send/Embed in Log ({log_channel_obj.mention})")
    if perms_needed: await interaction.response.send_message(f"❌ Bot lacks permissions ({', '.join(perms_needed)}).", ephemeral=True); return

    await interaction.response.defer(ephemeral=False)
    status_message = await interaction.followup.send(f"⏳ Starting scan: {channel.mention}, limit={limit}, Flag:{flag_duplicates}, Reply:{reply_to_duplicates}, Del:{delete_duplicates}, Log:{log_scan_duplicates}...", wait=True)
    processed_messages = 0; added_hashes = 0; updated_hashes = 0; flagged_count = 0; replied_count = 0; deleted_count = 0; logged_count = 0
    skipped_attachments = 0; errors = 0
    loop = asyncio.get_running_loop(); guild_db_updated = False
    if guild_id in active_hash_databases: del active_hash_databases[guild_id]
    stored_hashes = await load_guild_hashes(guild_id, loop)
    scanned_image_data = defaultdict(list)
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1: Gathering image data...")
    start_time_phase1 = datetime.datetime.now()
    try:
        async for message in channel.history(limit=limit):
            processed_messages += 1
            if processed_messages % SCAN_UPDATE_INTERVAL == 0:
                try:
                    await status_message.edit(content=f"⏳ Scanning... {processed_messages}/{limit} msgs (Phase 1).")
                except Exception as e:
                    print(f"DEBUG: Error editing status (Phase 1 Loop): {e}")
            if message.author.bot or not message.attachments: continue
            message_user_id = message.author.id; message_timestamp = message.created_at.replace(tzinfo=datetime.timezone.utc)
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    try:
                        image_bytes = await attachment.read(); img_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                        if img_hash is None: skipped_attachments += 1; continue
                        scanned_image_data[str(img_hash)].append((message_timestamp, message.id, message_user_id, attachment.filename, message))
                    except discord.HTTPException as e: print(f"Warning: [Scan G:{guild_id}] HTTP Fail download attach {attachment.id}: {e.status} {e.code}"); errors += 1; skipped_attachments += 1
                    except UnidentifiedImageError: print(f"Warning: [Scan G:{guild_id}] Skip unidentifiable image {attachment.id}"); skipped_attachments += 1
                    except Exception as e: print(f"Error: [Scan G:{guild_id}] Error processing attach {attachment.id}: {e}"); errors += 1; skipped_attachments += 1; traceback.print_exc()
    except discord.Forbidden:
        final_content = f"❌ Scan failed (Phase 1). No 'Read History' perm in {channel.mention}."
        try:
            await status_message.edit(content=final_content)
        except discord.HTTPException:
            try: # Nested try for fallback send
                await interaction.channel.send(content=final_content)
            except Exception as send_e: print(f"ERROR: Failed fallback send for Phase 1 Forbidden error: {send_e}")
        if guild_id in active_hash_databases: del active_hash_databases[guild_id];
        return
    except Exception as e:
        final_content = f"❌ Error during scan (Phase 1): {e}"
        try:
            await status_message.edit(content=final_content)
        except discord.HTTPException:
            try: # Nested try for fallback send
                await interaction.channel.send(content=final_content)
            except Exception as send_e: print(f"ERROR: Failed fallback send for Phase 1 Exception: {send_e}")
        traceback.print_exc();
        if guild_id in active_hash_databases: del active_hash_databases[guild_id];
        return
    elapsed_phase1 = (datetime.datetime.now() - start_time_phase1).total_seconds()
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1 complete ({elapsed_phase1:.2f}s). Found {len(scanned_image_data)} unique hash groups.")

    start_time_phase2 = datetime.datetime.now()
    try:
        await status_message.edit(content=f"⏳ Processing {len(scanned_image_data)} unique hash groups...")
    except Exception as e:
        print(f"DEBUG: Error editing status (Phase 2 Start): {e}")
    processed_hashes = 0
    for img_hash_str, entries in scanned_image_data.items():
        processed_hashes += 1
        if processed_hashes % (SCAN_UPDATE_INTERVAL * 2) == 0:
            status_text = (f"⏳ Processing... {processed_hashes}/{len(scanned_image_data)} hashes. Added:{added_hashes}, Updated:{updated_hashes}, Replied:{replied_count}, Flagged:{flagged_count}, Deleted:{deleted_count}, Logged:{logged_count}")
            try:
                await status_message.edit(content=status_text)
            except Exception as e:
                print(f"DEBUG: Error editing status (Phase 2 Loop): {e}")
        if not entries: continue
        entries.sort(key=lambda x: x[0]); oldest_entry = entries[0]
        oldest_timestamp, oldest_msg_id, oldest_user_id, oldest_filename, oldest_message_obj = oldest_entry
        oldest_identifier = f"{oldest_msg_id}-{oldest_filename}"
        try: img_hash_obj = imagehash.hex_to_hash(img_hash_str)
        except ValueError: print(f"Warning: [Scan G:{guild_id}] Invalid hash '{img_hash_str}', skipping."); errors += len(entries); continue
        existing_identifier, existing_data = find_existing_hash_entry_sync(img_hash_obj, stored_hashes, existence_threshold, current_scope, scan_channel_id_str)
        update_needed = False; identifier_to_use = oldest_identifier; data_to_use = {"hash": img_hash_str, "user_id": oldest_user_id, "timestamp": oldest_timestamp.isoformat()}; existing_time = None
        if existing_identifier:
            existing_timestamp_str = None
            if isinstance(existing_data, dict):
                existing_timestamp_str = existing_data.get('timestamp')
            if existing_timestamp_str:
                try:
                    existing_time = dateutil.parser.isoparse(existing_timestamp_str).replace(tzinfo=datetime.timezone.utc)
                    update_needed = oldest_timestamp < existing_time
                except Exception as parse_e:
                    print(f"Warning: [Scan G:{guild_id}] Error parsing DB time '{existing_timestamp_str}': {parse_e}. Update needed.")
                    update_needed = True
            else:
                update_needed = True # Existing entry has no timestamp (old format?), update it
            if not update_needed: # DB entry is older or same age
                identifier_to_use = existing_identifier
                data_to_use = existing_data
        else: # Hash not found in DB at all
            update_needed = True

        if update_needed:
            guild_db_updated = True
            if existing_identifier and oldest_timestamp < (existing_time or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)):
                updated_hashes += 1
            elif not existing_identifier:
                added_hashes += 1

            if current_scope == "server":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                if existing_identifier and existing_identifier != identifier_to_use:
                    stored_hashes.pop(existing_identifier, None)
                stored_hashes[identifier_to_use] = data_to_use
            elif current_scope == "channel":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                channel_hashes = stored_hashes.setdefault(scan_channel_id_str, {});
                if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[scan_channel_id_str] = channel_hashes
                if existing_identifier and existing_identifier != identifier_to_use:
                    channel_hashes.pop(existing_identifier, None)
                channel_hashes[identifier_to_use] = data_to_use

        canonical_oldest_id = identifier_to_use
        canonical_oldest_user_id = data_to_use.get('user_id')
        canonical_oldest_msg_obj = oldest_message_obj if identifier_to_use == oldest_identifier else None

        for entry_timestamp, entry_msg_id, entry_user_id, entry_filename, entry_message_obj in entries:
            if entry_msg_id == int(canonical_oldest_id.split('-')[0]): continue
            is_violation = False
            if current_mode == "strict":
                is_violation = True
            elif current_mode == "owner_allowed":
                is_violation = canonical_oldest_user_id is None or canonical_oldest_user_id != entry_user_id

            if is_violation and current_duration > 0:
                try:
                    canonical_oldest_time = dateutil.parser.isoparse(data_to_use.get('timestamp')).replace(tzinfo=datetime.timezone.utc)
                    if (entry_timestamp - canonical_oldest_time).days > current_duration:
                        is_violation = False
                except Exception:
                    is_violation = False # Error parsing time, assume not violation for safety

            if is_violation:
                action_taken = False
                if log_scan_duplicates and interaction.guild:
                    log_embed = discord.Embed(title="Duplicate Detected (Scan)", color=discord.Color.orange(), timestamp=datetime.datetime.now(datetime.timezone.utc))
                    log_embed.add_field(name="Dup User", value=f"<@{entry_user_id}> (`{entry_user_id}`)", inline=True)
                    log_embed.add_field(name="Channel", value=channel.mention, inline=True)
                    try:
                        log_embed.add_field(name="Dup Msg", value=f"[Link]({entry_message_obj.jump_url})", inline=True)
                    except: # Fallback if jump_url fails
                        log_embed.add_field(name="Dup Msg", value=f"`{entry_msg_id}`", inline=True)

                    log_embed.add_field(name="Hash", value=f"`{img_hash_str}`", inline=False)
                    log_embed.add_field(name="Match ID", value=f"`{canonical_oldest_id}`", inline=True)
                    log_embed.add_field(name="Dist", value="0", inline=True) # Scan implies exact hash match for grouping
                    orig_user_mention = f"<@{canonical_oldest_user_id}> (`{canonical_oldest_user_id}`)" if canonical_oldest_user_id else "Unknown"
                    log_embed.add_field(name="Orig User", value=orig_user_mention, inline=True)
                    original_msg_link = None
                    try:
                        original_msg_link = canonical_oldest_msg_obj.jump_url if canonical_oldest_msg_obj else f"https://discord.com/channels/{guild_id}/{scan_channel_id}/{int(canonical_oldest_id.split('-')[0])}"
                    except:
                        pass
                    if original_msg_link:
                        log_embed.add_field(name="Orig Msg", value=f"[Link]({original_msg_link})", inline=False)

                    thumb_url = next((att.url for att in entry_message_obj.attachments if att.content_type and att.content_type.startswith('image/')), None)
                    if thumb_url:
                        log_embed.set_thumbnail(url=thumb_url)

                    log_embed.set_footer(text=f"Scan by: {interaction.user.name} | Guild: {guild_id}")
                    await log_event(interaction.guild, log_embed)
                    logged_count += 1
                    action_taken = True

                if reply_on_scan_duplicate_config and reply_to_duplicates:
                    try:
                        template_data = { "mention": f"<@{entry_user_id}>", "filename": entry_filename, "identifier": canonical_oldest_id, "distance": 0, "original_user_mention": f"<@{canonical_oldest_user_id}>" if canonical_oldest_user_id else "*Unknown*", "emoji": duplicate_reaction_emoji, "original_user_info": f", Orig User: <@{canonical_oldest_user_id}>" if canonical_oldest_user_id else "", "jump_link": "" }
                        if canonical_oldest_msg_obj:
                            try:
                                template_data["jump_link"] = f"\nOriginal: {canonical_oldest_msg_obj.jump_url}"
                            except: pass
                        elif canonical_oldest_id:
                            try:
                                template_data["jump_link"] = f"\nOriginal: https://discord.com/channels/{guild_id}/{scan_channel_id}/{int(canonical_oldest_id.split('-')[0])}"
                            except: pass
                        reply_text = reply_template.format_map(defaultdict(str, template_data))
                        await entry_message_obj.reply(reply_text, mention_author=False)
                        replied_count += 1
                        action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan G:{guild_id}] No Send perm reply {entry_msg_id}."); break
                    except discord.NotFound: print(f"Warning: [Scan G:{guild_id}] Msg {entry_msg_id} not found reply.")
                    except Exception as e: print(f"DEBUG: [Scan G:{guild_id}] Failed reply {entry_msg_id}: {e}")

                if flag_duplicates:
                    try:
                        refreshed = await channel.fetch_message(entry_message_obj.id)
                        if not any(str(r.emoji) == duplicate_reaction_emoji and r.me for r in refreshed.reactions):
                            await entry_message_obj.add_reaction(duplicate_reaction_emoji)
                            flagged_count += 1
                            action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan G:{guild_id}] No React perm {entry_msg_id}."); break
                    except discord.NotFound: print(f"Warning: [Scan G:{guild_id}] Msg {entry_msg_id} not found react.")
                    except Exception as e: print(f"DEBUG: [Scan G:{guild_id}] Failed react {entry_msg_id}: {e}")

                if delete_duplicates:
                    try:
                        await entry_message_obj.delete()
                        deleted_count += 1
                        action_taken = True
                        print(f"DEBUG: [Scan G:{guild_id}] Deleted msg {entry_msg_id}")
                    except discord.Forbidden: print(f"Warning: [Scan G:{guild_id}] No Manage perm delete {entry_msg_id}."); break
                    except discord.NotFound: print(f"Warning: [Scan G:{guild_id}] Msg {entry_msg_id} not found delete.")
                    except Exception as e: print(f"DEBUG: [Scan G:{guild_id}] Failed delete {entry_msg_id}: {e}")

                if action_taken:
                    await asyncio.sleep(SCAN_ACTION_DELAY)

    if guild_db_updated:
        print(f"DEBUG: [Scan G:{guild_id}] Saving hash DB after scan...")
        if not await save_guild_hashes(guild_id, stored_hashes, loop):
            print(f"ERROR: [Scan G:{guild_id}] CRITICAL: Failed save hash DB!")
            try:
                await interaction.followup.send("⚠️ CRITICAL: Error saving hashes!", ephemeral=True)
            except discord.HTTPException:
                print(f"ERROR: [Scan G:{guild_id}] Failed save AND followup warn.")
        else:
            print(f"DEBUG: [Scan G:{guild_id}] Hash DB saved.")
    if guild_id in active_hash_databases: del active_hash_databases[guild_id]

    elapsed_phase2 = (datetime.datetime.now() - start_time_phase2).total_seconds()
    total_elapsed = (datetime.datetime.now() - start_time_phase1).total_seconds()
    print(f"DEBUG: [Scan G:{guild_id}] Phase 2 complete ({elapsed_phase2:.2f}s). Total: {total_elapsed:.2f}s.")
    final_message_content = (
        f"✅ Scan Complete! Processed {processed_messages} messages ({total_elapsed:.2f}s). "
        f"Added:{added_hashes}, Updated:{updated_hashes}, Replied:{replied_count}, "
        f"Flagged:{flagged_count}, Deleted:{deleted_count}, Logged:{logged_count}. "
        f"Skipped:{skipped_attachments}. Errors:{errors}."
    )
    try:
        await status_message.edit(content=final_message_content)
        print(f"DEBUG: [Scan G:{guild_id}] Edited final status.")
    except discord.HTTPException as e:
        print(f"DEBUG: [Scan G:{guild_id}] Edit status failed (Status: {e.status}, Code: {e.code}). Sending new.");
        if e.code == 50027 or e.status == 401:
            try:
                try:
                    await status_message.delete()
                except Exception: pass # Ignore delete errors
                if interaction.channel:
                    await interaction.channel.send(content=final_message_content)
                print(f"DEBUG: [Scan G:{guild_id}] Sent final status new.")
            except discord.Forbidden:
                print(f"ERROR: [Scan G:{guild_id}] No Send/Delete perm in {channel.mention}.")
            except Exception as send_e:
                print(f"ERROR: [Scan G:{guild_id}] Failed send final status new: {send_e}"); traceback.print_exc()
        else: print(f"ERROR: [Scan G:{guild_id}] Unexpected HTTP error edit final status: {e}"); traceback.print_exc()
    except Exception as final_edit_err:
        print(f"ERROR: [Scan G:{guild_id}] Non-HTTP error edit final status: {final_edit_err}"); traceback.print_exc();
        try:
            if interaction.channel:
                await interaction.channel.send(content=final_message_content)
        except Exception: pass # Best effort fallback


# --- Clear Flags Command ---
@bot.slash_command(name="clearflags", description="Removes the bot's duplicate warning reactions from messages.")
async def clear_flags(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "Channel to clear."), confirm: discord.Option(bool, "Confirm clearing?"), limit: discord.Option(int, "Max messages.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 Set `confirm:True` to proceed.", ephemeral=True); return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild.", ephemeral=True); return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id); duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️'); bot_member = interaction.guild.me
    if not channel.permissions_for(bot_member).read_message_history: await interaction.response.send_message(f"❌ Bot lacks 'Read History' in {channel.mention}.", ephemeral=True); return
    if not channel.permissions_for(bot_member).add_reactions: await interaction.response.send_message(f"❌ Bot lacks 'Add Reactions' in {channel.mention}.", ephemeral=True); return
    await interaction.response.defer(ephemeral=False)
    status_message = await interaction.followup.send(f"⏳ Starting reaction cleanup ({duplicate_reaction_emoji}) in {channel.mention} (limit {limit})...", wait=True)
    processed_msgs = 0; reactions_removed = 0; errors = 0; final_status_content = ""; start_time = datetime.datetime.now()
    try:
        async for message in channel.history(limit=limit):
            processed_msgs += 1
            if processed_msgs % (SCAN_UPDATE_INTERVAL * 2) == 0:
                try:
                    await status_message.edit(content=f"⏳ Clearing... Checked {processed_msgs}/{limit}. Removed {reactions_removed}.")
                except Exception as e:
                    print(f"DEBUG: Error editing status (ClearFlags): {e}")
            reaction_to_remove = next((r for r in message.reactions if str(r.emoji) == duplicate_reaction_emoji and r.me), None)
            if reaction_to_remove:
                try: await message.remove_reaction(reaction_to_remove.emoji, bot_member); reactions_removed += 1; await asyncio.sleep(CLEAR_REACTION_DELAY)
                except discord.Forbidden: print(f"Warning: [ClearFlags G:{guild_id}] No perm remove reaction {channel.mention}. Stop."); errors += 1; final_status_content = f"⚠️ Stopped due to perm error. Checked {processed_msgs}. Removed {reactions_removed}."; break
                except discord.NotFound: pass
                except Exception as e: print(f"Error: [ClearFlags G:{guild_id}] Failed remove reaction {message.id}: {e}"); errors += 1
        if not final_status_content:
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            final_status_content = (f"✅ Cleanup Complete! Checked {processed_msgs} messages ({elapsed:.2f}s). Removed **{reactions_removed}** reactions. Errors: {errors}.")
    except discord.Forbidden: final_status_content = f"❌ Cleanup failed. No 'Read History' perm in {channel.mention}."; errors += 1
    except Exception as e: final_status_content = f"❌ Error during cleanup: {e}"; traceback.print_exc(); errors += 1
    finally:
        try: await status_message.edit(content=final_status_content)
        except discord.HTTPException as e_http:
            print(f"DEBUG: [ClearFlags G:{guild_id}] Edit final status failed (Status: {e_http.status}, Code: {e_http.code}). Sending new.")
            if e_http.code == 50027 or e_http.status == 401:
                try:
                    try:
                        await status_message.delete()
                    except Exception: pass # Ignore delete errors
                    if interaction.channel:
                        await interaction.channel.send(content=final_status_content)
                except Exception as e_send:
                    print(f"ERROR: [ClearFlags G:{guild_id}] Failed send final status new: {e_send}")
            else: print(f"ERROR: [ClearFlags G:{guild_id}] Unexpected HTTP error edit final status: {e_http}")
        except Exception as e_final:
            print(f"ERROR: [ClearFlags G:{guild_id}] Non-HTTP error edit final status: {e_final}")
            try:
                if interaction.channel:
                    await interaction.channel.send(content=final_status_content)
            except Exception: pass # Best effort fallback


# --- Main Execution ---
if __name__ == "__main__":
    try: import dateutil.parser
    except ImportError: print("Optional dependency 'python-dateutil' not found. Timestamps might not parse correctly. Consider: pip install python-dateutil")
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found in environment variables or .env file.", file=sys.stderr); sys.exit(1)
    try: print("Starting bot..."); bot.run(BOT_TOKEN)
    except discord.LoginFailure: print("Error: Improper token passed. Ensure DISCORD_BOT_TOKEN is correct.", file=sys.stderr); sys.exit(1)
    except discord.PrivilegedIntentsRequired: print("Error: Privileged Intents (Message Content) are not enabled. Go to the Discord Developer Portal -> Your App -> Bot -> Enable Message Content Intent.", file=sys.stderr); sys.exit(1)
    except Exception as e: print(f"An error occurred while starting or running the bot: {e}", file=sys.stderr); traceback.print_exc(); sys.exit(1)
    finally: print("--- Bot process ended. ---")

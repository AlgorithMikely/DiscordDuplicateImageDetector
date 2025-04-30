#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate images using Slash Commands.
Supports per-server configuration, scope, check mode, time limits,
history scanning (/scan), hash management, user allowlisting,
retroactive flag clearing (/clearflags), configurable replies (with on/off switch),
and logging to a designated channel.
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
# DEFAULT_COMMAND_PREFIX = "!" # No longer needed for slash commands
HASH_FILENAME_PREFIX = "hashes_"
VALID_SCOPES = ["server", "channel"]
VALID_CHECK_MODES = ["strict", "owner_allowed"]
DEFAULT_SCAN_LIMIT = 1000
SCAN_UPDATE_INTERVAL = 100 # How often to update status message during scan
SCAN_ACTION_DELAY = 0.35 # Delay between actions on duplicates during scan
CLEAR_REACTION_DELAY = 0.2 # Delay between clearing reactions

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

# --- Configuration Loading/Saving ---

def get_default_guild_config(guild_id):
    """Returns default settings, including allowlist, reply template, log channel, and reply toggle."""
    return {
        "hash_db_file": f"{HASH_FILENAME_PREFIX}{guild_id}.json",
        "hash_size": 8,
        "similarity_threshold": 5,
        "allowed_channel_ids": None,
        "react_to_duplicates": True,
        "delete_duplicates": False,
        "reply_on_duplicate": True, # Controls replies for NEW posts
        "duplicate_reaction_emoji": "⚠️",
        "duplicate_scope": "server",
        "duplicate_check_mode": "strict",
        "duplicate_check_duration_days": 0, # 0 = check forever
        "allowed_users": [], # List of user IDs exempt from checks
        "duplicate_reply_template": DEFAULT_REPLY_TEMPLATE,
        "log_channel_id": None # Channel ID for logging events
    }

def validate_config_data(config_data):
    """Validates config, including new fields."""
    validated = get_default_guild_config(0).copy()
    validated.update(config_data)
    try:
        # Coerce types
        validated['hash_size'] = int(validated['hash_size'])
        validated['similarity_threshold'] = int(validated['similarity_threshold'])
        validated['react_to_duplicates'] = bool(validated['react_to_duplicates'])
        validated['delete_duplicates'] = bool(validated['delete_duplicates'])
        validated['reply_on_duplicate'] = bool(validated.get('reply_on_duplicate', True))
        validated['duplicate_check_duration_days'] = int(validated.get('duplicate_check_duration_days', 0))
        if validated['duplicate_check_duration_days'] < 0: validated['duplicate_check_duration_days'] = 0

        # Validate enums
        if validated.get('duplicate_scope') not in VALID_SCOPES: validated['duplicate_scope'] = "server"
        if validated.get('duplicate_check_mode') not in VALID_CHECK_MODES: validated['duplicate_check_mode'] = "strict"

        # Validate allowed_channel_ids (list of ints or None)
        if validated['allowed_channel_ids'] is not None:
            if isinstance(validated['allowed_channel_ids'], list):
                validated['allowed_channel_ids'] = [int(ch_id) for ch_id in validated['allowed_channel_ids'] if str(ch_id).isdigit()]
                if not validated['allowed_channel_ids']: validated['allowed_channel_ids'] = None
            else: validated['allowed_channel_ids'] = None

        # Validate allowed_users (list of ints or empty list)
        if 'allowed_users' not in validated or not isinstance(validated['allowed_users'], list):
             validated['allowed_users'] = []
        else:
             validated['allowed_users'] = [int(u_id) for u_id in validated['allowed_users'] if str(u_id).isdigit()]

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
        # print(f"DEBUG: Loading main config file: {CONFIG_FILE_PATH}") # Can be noisy
        try:
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
            if not isinstance(loaded_data, dict): raise TypeError("Config is not a dictionary")
            validated_configs = {}
            for guild_id_str, guild_config_data in loaded_data.items():
                try:
                    guild_id = int(guild_id_str)
                    validated_configs[guild_id] = validate_config_data(guild_config_data)
                    validated_configs[guild_id]['hash_db_file'] = f"{HASH_FILENAME_PREFIX}{guild_id}.json"
                except ValueError: print(f"Warning: Invalid guild ID '{guild_id_str}'. Skipping.", file=sys.stderr)
            server_configs = validated_configs
            print(f"Successfully loaded configurations for {len(server_configs)} guilds.")
        except FileNotFoundError: print(f"Info: Config file '{CONFIG_FILE_PATH}' not found."); server_configs = {}
        except Exception as e: print(f"Error loading main config: {e}"); server_configs = {}

async def save_main_config():
    """Saves the global server_configs cache."""
    async with config_lock:
        # print(f"DEBUG: Saving main config file: {CONFIG_FILE_PATH}") # Can be noisy
        config_to_save = {str(gid): data for gid, data in server_configs.items()}
        try:
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f: json.dump(config_to_save, f, indent=4)
            # print(f"DEBUG: Successfully saved main config for {len(config_to_save)} guilds.") # Can be noisy
            return True
        except Exception as e: print(f"DEBUG: Error saving main config: {e}"); return False

def get_guild_config(guild_id):
    """Gets guild config, ensures defaults exist (incl. new fields)."""
    global server_configs
    defaults_needed = False
    if guild_id not in server_configs:
        server_configs[guild_id] = get_default_guild_config(guild_id)
        defaults_needed = True
    else:
        guild_conf = server_configs[guild_id]
        default_conf = get_default_guild_config(guild_id)
        updated = False
        for key, default_value in default_conf.items():
            # Ensure all default keys exist in the loaded config
            if key not in guild_conf:
                 guild_conf[key] = default_value; updated = True
        if updated:
             # If keys were added, re-validate the whole config dict
             server_configs[guild_id] = validate_config_data(guild_conf)
             defaults_needed = True # Mark that save is needed

    if defaults_needed: asyncio.create_task(save_main_config()) # Save if defaults added/updated
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
    except UnidentifiedImageError: return None # Handle PIL errors specifically
    except Exception: return None # Catch other potential errors
async def calculate_hash(image_bytes, hash_size, loop):
    func = partial(calculate_hash_sync, image_bytes, hash_size); return await loop.run_in_executor(None, func)

def load_hashes_sync(db_file):
    if not os.path.exists(db_file): return {}
    try:
        with open(db_file, 'r', encoding='utf-8') as f: data = json.load(f)
        if not isinstance(data, dict): return {}
        # Simple format check - can be enhanced
        is_new_format_likely = False; has_timestamp = False
        if data:
            first_val = next(iter(data.values()), None)
            if isinstance(first_val, dict) and 'hash' in first_val and 'user_id' in first_val:
                is_new_format_likely = True
                if 'timestamp' in first_val: has_timestamp = True
            elif isinstance(first_val, dict): # Check nested dict (channel scope)
                 nested_val = next(iter(first_val.values()), None)
                 if isinstance(nested_val, dict) and 'hash' in nested_val and 'user_id' in nested_val:
                    is_new_format_likely = True
                    if 'timestamp' in nested_val: has_timestamp = True

        if is_new_format_likely and not has_timestamp and data: print(f"Warning: Hash file '{db_file}' seems to use new format but is missing timestamps.", file=sys.stderr)
        elif not is_new_format_likely and data: print(f"Warning: Hash file '{db_file}' seems to use old format (just hash strings).", file=sys.stderr)
        return data
    except json.JSONDecodeError as e: print(f"DEBUG: Error decoding JSON from hash db '{db_file}': {e}"); return {}
    except Exception as e: print(f"DEBUG: Error loading hash db '{db_file}': {e}"); return {}

def save_hashes_sync(hashes_dict, db_file):
    try:
        with open(db_file, 'w', encoding='utf-8') as f: json.dump(hashes_dict, f, indent=4)
        return True
    except Exception as e: print(f"DEBUG: Error saving hash db '{db_file}': {e}"); return False

async def load_guild_hashes(guild_id, loop):
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(load_hashes_sync, db_file); return await loop.run_in_executor(None, func)

async def save_guild_hashes(guild_id, hashes_dict, loop):
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(save_hashes_sync, hashes_dict, db_file); return await loop.run_in_executor(None, func)

# --- Duplicate Finding ---

def find_existing_hash_entry_sync(target_hash, stored_hashes_dict, threshold, scope, channel_id_str):
    """
    Finds the first entry matching the hash within the threshold and scope.
    Returns (identifier, hash_data) tuple or (None, None).
    Handles old and new hash data formats.
    """
    if target_hash is None: return None, None
    hashes_to_check = {}
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return None, None # Invalid scope

    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        if isinstance(hash_data, dict) and 'hash' in hash_data: stored_hash_str = hash_data.get('hash')
        elif isinstance(hash_data, str): stored_hash_str = hash_data # Handle old format

        if stored_hash_str is None: continue

        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            if (target_hash - stored_hash) <= threshold:
                # Return the full data structure (dict or str) associated with the identifier
                return identifier, hash_data
        except ValueError: continue # Ignore invalid hash strings in the DB
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}' against '{target_hash}': {e}", file=sys.stderr)

    return None, None # No match found

def find_duplicates_sync(new_image_hash, stored_hashes_dict, threshold, scope, channel_id_str, check_duration_days):
    """Finds recent duplicates based on scope and time duration."""
    duplicates = []
    if new_image_hash is None: return duplicates
    hashes_to_check = {}
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return duplicates # Invalid scope

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff_time = now - datetime.timedelta(days=check_duration_days) if check_duration_days > 0 else None

    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None; original_user_id = None; timestamp_str = None
        if isinstance(hash_data, dict): # New format
            stored_hash_str = hash_data.get('hash'); original_user_id = hash_data.get('user_id'); timestamp_str = hash_data.get('timestamp')
        elif isinstance(hash_data, str): # Old format
            stored_hash_str = hash_data
            # Cannot check timestamp or user for old format
            if check_duration_days > 0: continue # Skip if time limit is set, as we can't verify
        else: continue # Skip invalid entries

        if stored_hash_str is None: continue

        # Time Check (only for new format with timestamp)
        if cutoff_time and timestamp_str:
            try:
                stored_time = dateutil.parser.isoparse(timestamp_str)
                if stored_time.tzinfo is None: stored_time = stored_time.replace(tzinfo=datetime.timezone.utc)
                if stored_time < cutoff_time: continue # Skip if older than cutoff
            except Exception: continue # Skip if timestamp is invalid

        # Hash Comparison
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = new_image_hash - stored_hash
            if distance <= threshold:
                original_message_id = None
                try: original_message_id = int(identifier.split('-')[0])
                except: pass # Identifier might be different format if manually added
                duplicates.append({
                    'identifier': identifier,
                    'distance': distance,
                    'original_message_id': original_message_id,
                    'original_user_id': original_user_id # Will be None for old format
                })
        except ValueError: continue # Ignore invalid stored hash string
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}' against '{new_image_hash}': {e}", file=sys.stderr)

    duplicates.sort(key=lambda x: x['distance']) # Sort by similarity
    return duplicates

async def find_duplicates(new_image_hash, stored_hashes_dict, threshold, scope, channel_id, check_duration_days, loop):
    """Async wrapper for duplicate finding."""
    func = partial(find_duplicates_sync, new_image_hash, stored_hashes_dict, threshold, scope, str(channel_id), check_duration_days)
    duplicates = await loop.run_in_executor(None, func)
    return duplicates

# --- Logging Helper ---
async def log_event(guild: discord.Guild, embed: discord.Embed):
    """Sends an embed message to the configured log channel."""
    if not guild: return # Cannot log without guild context
    guild_config = get_guild_config(guild.id)
    log_channel_id = guild_config.get('log_channel_id')

    if log_channel_id:
        try:
            log_channel = bot.get_channel(log_channel_id) or await bot.fetch_channel(log_channel_id)
            if isinstance(log_channel, discord.TextChannel):
                 # Check permissions before sending
                 if log_channel.permissions_for(guild.me).send_messages and log_channel.permissions_for(guild.me).embed_links:
                     await log_channel.send(embed=embed)
                 else:
                     print(f"Warning: [LogEvent G:{guild.id}] Missing Send Messages/Embed Links permission in log channel {log_channel_id}.")
            else:
                 print(f"Warning: [LogEvent G:{guild.id}] Configured log channel {log_channel_id} is not a text channel.")
        except discord.NotFound:
            print(f"Warning: [LogEvent G:{guild.id}] Configured log channel {log_channel_id} not found.")
        except discord.Forbidden:
            print(f"Warning: [LogEvent G:{guild.id}] Bot lacks permission to access log channel {log_channel_id}.")
        except Exception as e:
            print(f"Error: [LogEvent G:{guild.id}] Failed to send log message to channel {log_channel_id}: {e}")


# --- Discord Bot Implementation ---
intents = discord.Intents.default(); intents.message_content = True; intents.guilds = True; intents.members = True; intents.reactions = True
bot = discord.Bot(intents=intents)

# --- Event Handlers ---
@bot.event
async def on_ready():
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---')
    await load_main_config()
    print(f'--- Ready for {len(bot.guilds)} guilds ---')
    for guild in bot.guilds: _ = get_guild_config(guild.id) # Ensure default config exists for all guilds
    try:
        print("--- Syncing slash commands... ---")
        await bot.sync_commands()
        print("--- Slash commands synced ---")
    except Exception as e: print(f"--- Failed to sync slash commands: {e} ---")
    print('------ Bot is ready! ------')

@bot.event
async def on_guild_join(guild):
     print(f"Joined new guild: {guild.name} (ID: {guild.id})"); _ = get_guild_config(guild.id); await save_main_config()

@bot.event
async def on_message(message):
    """Handles image processing (NOT commands)."""
    if message.guild is None or message.author == bot.user or message.author.bot: return
    guild_id = message.guild.id; guild_config = get_guild_config(guild_id)
    current_user_id = message.author.id; allowed_users = guild_config.get('allowed_users', [])
    if current_user_id in allowed_users: return # Skip if allowlisted

    # --- Image Processing Logic ---
    channel_id = message.channel.id; channel_id_str = str(channel_id)
    allowed_channel_ids = guild_config.get('allowed_channel_ids')
    current_scope = guild_config.get('duplicate_scope', 'server')
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    current_hash_size = guild_config.get('hash_size', 8)
    current_similarity_threshold = guild_config.get('similarity_threshold', 5)
    react_to_duplicates = guild_config.get('react_to_duplicates', True)
    delete_duplicates = guild_config.get('delete_duplicates', False)
    reply_on_duplicate = guild_config.get('reply_on_duplicate', True) # Controls reply for NEW messages
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE)

    # Check if channel is monitored
    if allowed_channel_ids and channel_id not in allowed_channel_ids: return
    if not message.attachments: return

    loop = asyncio.get_running_loop()
    stored_hashes = await load_guild_hashes(guild_id, loop)
    db_updated = False

    for i, attachment in enumerate(message.attachments):
        # Check if it looks like an image
        if not (attachment.content_type and attachment.content_type.startswith('image/')):
            continue

        try:
            image_bytes = await attachment.read()
            new_hash = await calculate_hash(image_bytes, current_hash_size, loop)
            if new_hash is None:
                print(f"Debug: [G:{guild_id}] Could not calculate hash for attachment {attachment.filename} in msg {message.id}")
                continue

            duplicate_matches = await find_duplicates(
                new_hash, stored_hashes, current_similarity_threshold,
                current_scope, channel_id, current_duration, loop
            )

            is_violation = False; violating_match = None
            if duplicate_matches:
                if current_mode == "strict": is_violation = True; violating_match = duplicate_matches[0]
                elif current_mode == "owner_allowed":
                    for match in duplicate_matches:
                        # Violation if original user is unknown OR different from current user
                        if match.get('original_user_id') is None or match.get('original_user_id') != current_user_id:
                            is_violation = True; violating_match = match; break

            # --- Handle Violation ---
            if is_violation and violating_match:
                identifier = violating_match['identifier']; distance = violating_match['distance']
                original_message_id = violating_match.get('original_message_id'); original_user_id = violating_match.get('original_user_id')
                original_msg_link = None # Define for logging scope

                # --- Send Reply (if enabled) ---
                if reply_on_duplicate:
                    template_data = {
                        "mention": message.author.mention,
                        "filename": attachment.filename,
                        "identifier": identifier,
                        "distance": distance,
                        "original_user_mention": f"<@{original_user_id}>" if original_user_id else "*Unknown*",
                        "emoji": duplicate_reaction_emoji,
                        "original_user_info": f", Orig User: <@{original_user_id}>" if original_user_id else "",
                        "jump_link": ""
                    }
                    if original_message_id and message.guild:
                        try:
                            # Attempt to build jump link (might be wrong channel if cross-channel check)
                            jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                            template_data["jump_link"] = f"\nOriginal: {jump_url}"
                            original_msg_link = jump_url
                        except Exception as e: print(f"DEBUG: Failed to build jump link: {e}")
                    reply_text = reply_template.format_map(defaultdict(str, template_data))
                    try:
                         await message.reply(reply_text, mention_author=True)
                    except discord.HTTPException as e: print(f"Error: Failed to reply to message {message.id}: {e}")

                # --- Log Violation ---
                log_embed = discord.Embed(
                    title="Duplicate Image Detected",
                    color=discord.Color.orange(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc)
                )
                log_embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
                log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                log_embed.add_field(name="Message", value=f"[Jump Link]({message.jump_url})", inline=True)
                log_embed.add_field(name="Image Hash", value=f"`{new_hash}`", inline=False)
                log_embed.add_field(name="Match Identifier", value=f"`{identifier}`", inline=True)
                log_embed.add_field(name="Hash Distance", value=str(distance), inline=True)
                orig_user_mention = f"<@{original_user_id}> (`{original_user_id}`)" if original_user_id else "Unknown"
                log_embed.add_field(name="Original User", value=orig_user_mention, inline=True)
                # Re-calculate approx link if reply was disabled
                if not original_msg_link and original_message_id and message.guild:
                     try: original_msg_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                     except: pass
                if original_msg_link:
                     log_embed.add_field(name="Original Message", value=f"[Approx. Link]({original_msg_link})", inline=False)
                if attachment.url:
                     log_embed.set_thumbnail(url=attachment.url)
                log_embed.set_footer(text=f"Guild ID: {guild_id}")
                await log_event(message.guild, log_embed) # Send log

                # --- Perform Other Actions ---
                if react_to_duplicates:
                    try: await message.add_reaction(duplicate_reaction_emoji)
                    except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed reaction on msg {message.id}: {e}")
                if delete_duplicates:
                    if message.channel.permissions_for(message.guild.me).manage_messages:
                        try: await message.delete()
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed delete on msg {message.id}: {e}")
                    else: print(f"DEBUG: [G:{guild_id}] Lacking 'Manage Messages' permission to delete msg {message.id}.")

            # --- Add Unique Hash (if no violation) ---
            elif not is_violation:
                # Check if this exact hash already exists before adding
                # Use existence_threshold = 0 for exact match check
                existing_identifier, _ = find_existing_hash_entry_sync(new_hash, stored_hashes, 0, current_scope, channel_id_str)
                if not existing_identifier: # Only add if truly new
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
                    # print(f"Debug: Added new hash {new_hash} with ID {unique_identifier}") # Optional debug log

        except discord.HTTPException as e:
             print(f"Warning: [G:{guild_id}] HTTP error processing attachment '{attachment.filename}' (Msg ID: {message.id}): {e.status} {e.code}", file=sys.stderr)
        except Exception as e:
             print(f"Error: [G:{guild_id}] Unexpected error processing attachment '{attachment.filename}' (Msg ID: {message.id}): {e}", file=sys.stderr)
             traceback.print_exc()

    # Save hashes if any were added
    if db_updated:
        if not await save_guild_hashes(guild_id, stored_hashes, loop):
            print(f"ERROR: [G:{guild_id}] Failed to save updated hash database!")


# --- Slash Command Definitions ---

# Helper for permission checks
async def check_admin_permissions(interaction: discord.Interaction) -> bool:
    """Checks if the invoking user has administrator permissions."""
    if not isinstance(interaction.user, discord.Member):
         # Cannot check permissions if not in a guild context (e.g., DM)
         await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True); return False
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permissions to use this command.", ephemeral=True); return False
    return True

# --- Config Commands (Now Top-Level & Specific) ---

@bot.slash_command(name="config_view", description="Shows the current bot configuration for this server.")
async def config_view(interaction: discord.Interaction):
    """Displays the current server configuration."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    embed = discord.Embed(title=f"Bot Configuration for {interaction.guild.name}", color=discord.Color.blue())
    scope = guild_config.get('duplicate_scope', 'server'); mode = guild_config.get('duplicate_check_mode', 'strict')
    duration = guild_config.get('duplicate_check_duration_days', 0); duration_str = f"{duration} days" if duration > 0 else "Forever"
    log_channel_id = guild_config.get('log_channel_id'); log_channel_mention = f"<#{log_channel_id}>" if log_channel_id else "Not Set"

    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True); embed.add_field(name="Check Mode", value=f"`{mode}`", inline=True); embed.add_field(name="Check Duration", value=f"`{duration_str}`", inline=True)
    embed.add_field(name="Log Channel", value=log_channel_mention, inline=False) # Added log channel explicitly here

    for key, value in guild_config.items():
        # Skip keys already displayed or internal like hash_db_file
        if key in ['duplicate_scope', 'duplicate_check_mode', 'duplicate_check_duration_days', 'log_channel_id', 'hash_db_file']: continue

        display_value = value
        key_title = key.replace('_', ' ').title()

        if key == 'allowed_channel_ids': display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels"
        elif key == 'allowed_users': display_value = ', '.join(f'<@{u_id}>' for u_id in value) if value else "None"
        elif key == 'duplicate_reply_template': display_value = f"```\n{value}\n```"
        elif isinstance(value, bool): display_value = "Enabled" if value else "Disabled"
        else: display_value = f"`{value}`" # Default wrap in backticks

        embed.add_field(name=key_title, value=str(display_value), inline=isinstance(value, (int, bool))) # Inline some basic types

    await interaction.response.send_message(embed=embed, ephemeral=True)

async def _update_config(interaction: discord.Interaction, setting: str, new_value: typing.Any):
    """Helper to update and save config, sending response."""
    if not interaction.guild_id: return False # Should not happen with checks, but safeguard
    guild_id = interaction.guild_id
    guild_config = get_guild_config(guild_id).copy()
    original_value = guild_config.get(setting)

    # Prevent setting same value
    if original_value == new_value:
         await interaction.response.send_message(f"ℹ️ Setting '{setting}' is already set to `{new_value}`.", ephemeral=True)
         return False

    guild_config[setting] = new_value
    if await save_guild_config(guild_id, guild_config):
        # Format display values nicely
        display_orig = original_value
        display_new = new_value
        if setting == 'duplicate_check_duration_days':
             display_orig = f"{original_value} days" if original_value > 0 else "Forever"
             display_new = f"{new_value} days" if new_value > 0 else "Forever"
        elif setting == 'duplicate_reply_template':
             display_orig = f"```\n{original_value}\n```" if original_value else "`Not Set`"
             display_new = f"```\n{new_value}\n```"
        elif setting == 'log_channel_id':
             display_orig = f"<#{original_value}>" if original_value else "`None`"
             display_new = f"<#{new_value}>" if new_value else "`None`"
        elif isinstance(new_value, bool):
            display_orig = "`Enabled`" if original_value else "`Disabled`"
            display_new = "`Enabled`" if new_value else "`Disabled`"

        await interaction.response.send_message(f"✅ Updated '{setting}' from {display_orig} to {display_new}.", ephemeral=True)
        if setting == 'duplicate_scope': await interaction.followup.send(f"⚠️ **Warning:** Changing scope might affect hash lookup if hashes already exist.", ephemeral=True)
        return True
    else:
        await interaction.response.send_message(f"⚠️ Failed to save config update for '{setting}'.", ephemeral=True)
        return False

# --- Individual Config Set Commands ---

@bot.slash_command(name="config_set_threshold", description="Sets the similarity threshold (0-20, lower is stricter).")
async def config_set_threshold(interaction: discord.Interaction, value: discord.Option(int, "New threshold value (0=exact match).", min_value=0, max_value=20)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await _update_config(interaction, "similarity_threshold", value)

@bot.slash_command(name="config_set_hash_size", description="Sets the hash detail level (e.g., 8 or 16). Higher needs more CPU.")
async def config_set_hash_size(interaction: discord.Interaction, value: discord.Option(int, "New hash size (must be power of 2, >= 4).", min_value=4, max_value=32)):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     # Optional: Add check if value is power of 2? imagehash might handle it.
     await _update_config(interaction, "hash_size", value)

@bot.slash_command(name="config_set_react", description="Enable/disable reacting to newly posted duplicate messages.")
async def config_set_react(interaction: discord.Interaction, value: discord.Option(bool, "True to enable reactions, False to disable.")):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "react_to_duplicates", value)

@bot.slash_command(name="config_set_delete", description="Enable/disable deleting newly posted duplicate messages.")
async def config_set_delete(interaction: discord.Interaction, value: discord.Option(bool, "True to enable deletion, False to disable.")):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "delete_duplicates", value)

@bot.slash_command(name="config_set_reply", description="Enable/disable replying to newly posted duplicate messages.")
async def config_set_reply(interaction: discord.Interaction, value: discord.Option(bool, "True to enable replies, False to disable.")):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "reply_on_duplicate", value)

@bot.slash_command(name="config_set_emoji", description="Sets the emoji used for duplicate reactions.")
async def config_set_emoji(interaction: discord.Interaction, value: discord.Option(str, "The new emoji (standard or custom).")):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     # Test emoji validity
     try:
         await interaction.response.defer(ephemeral=True) # Defer first
         test_msg = await interaction.followup.send("Testing emoji...", ephemeral=True) # Send test message
         await test_msg.add_reaction(value) # Try reacting
         await test_msg.delete() # Clean up test message
     except discord.HTTPException as e:
         await interaction.edit_original_response(content=f"❌ Invalid or inaccessible emoji provided. Error: {e.text}")
         return
     except Exception as e:
         await interaction.edit_original_response(content=f"❌ An error occurred while testing the emoji: {e}")
         return

     # If test passed, update config (response handled by _update_config)
     await _update_config(interaction, "duplicate_reaction_emoji", value)


@bot.slash_command(name="config_set_scope", description="Sets duplicate check scope (server or channel).")
async def config_set_scope(interaction: discord.Interaction, value: discord.Option(str, "Choose scope.", choices=VALID_SCOPES)):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "duplicate_scope", value)

@bot.slash_command(name="config_set_check_mode", description="Sets duplicate check mode (strict or owner_allowed).")
async def config_set_check_mode(interaction: discord.Interaction, value: discord.Option(str, "Choose check mode.", choices=VALID_CHECK_MODES)):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "duplicate_check_mode", value)

@bot.slash_command(name="config_set_duration", description="Sets how many days back to check for duplicates (0=forever).")
async def config_set_duration(interaction: discord.Interaction, value: discord.Option(int, "Number of days (0 for forever).", min_value=0)):
     if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
     if not await check_admin_permissions(interaction): return
     await _update_config(interaction, "duplicate_check_duration_days", value)

@bot.slash_command(name="config_set_log_channel", description="Sets or clears the channel for logging duplicate detections.")
async def config_set_log_channel(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "The channel to send logs to, or omit to disable logging.", required=False, default=None)):
    """Sets or clears the logging channel."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild context.", ephemeral=True); return

    new_channel_id = channel.id if channel else None
    # Check bot permissions in the target channel if setting one
    if new_channel_id:
        log_channel = interaction.guild.get_channel(new_channel_id)
        if not log_channel or not isinstance(log_channel, discord.TextChannel):
              await interaction.response.send_message("❌ Invalid channel specified or not found in this server.", ephemeral=True); return
        bot_member = interaction.guild.me
        if not log_channel.permissions_for(bot_member).send_messages or not log_channel.permissions_for(bot_member).embed_links:
              await interaction.response.send_message(f"❌ Bot lacks 'Send Messages' or 'Embed Links' permission in {channel.mention}.", ephemeral=True); return
        # Send confirmation to the new log channel
        try:
            confirm_embed = discord.Embed(description=f"✅ This channel has been set as the duplicate log channel by {interaction.user.mention}.", color=discord.Color.green())
            await log_channel.send(embed=confirm_embed)
        except Exception as e:
             print(f"Warning: [G:{interaction.guild_id}] Failed to send confirmation message to new log channel {new_channel_id}: {e}")


    # Update config (response handled by _update_config)
    await _update_config(interaction, "log_channel_id", new_channel_id)


@bot.slash_command(name="config_set_reply_template", description="Sets the template for duplicate reply messages.")
async def config_set_reply_template(interaction: discord.Interaction, template: discord.Option(str, "The template string. See help/README for placeholders.")):
    """Sets the duplicate reply template."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not isinstance(template, str): await interaction.response.send_message("❌ Template must be a string.", ephemeral=True); return # Should be handled by Discord's typing
    if len(template) > 1500: await interaction.response.send_message("❌ Template is too long (max 1500 chars).", ephemeral=True); return

    # Basic placeholder validation (check if curly braces seem balanced) - rudimentary
    if template.count('{') != template.count('}'):
        await interaction.response.send_message("⚠️ Warning: Template has unbalanced curly braces `{}`. Check your placeholders.", ephemeral=True)
        # Still allow setting it, but warn the user.

    # Update config (response handled by _update_config)
    if await _update_config(interaction, "duplicate_reply_template", template):
        # Send placeholder info in followup if update was successful
        await interaction.followup.send(f"ℹ️ Placeholders: `{{mention}}`, `{{filename}}`, `{{identifier}}`, `{{distance}}`, `{{original_user_mention}}`, `{{emoji}}`, `{{original_user_info}}`, `{{jump_link}}`", ephemeral=True)


# --- Config Channel Commands (Now Top-Level) ---

@bot.slash_command(name="config_channel_view", description="Shows the list of channels currently monitored.")
async def config_channel_view(interaction: discord.Interaction):
    """Displays the allowed channels."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id); channel_list = guild_config.get('allowed_channel_ids')
    if channel_list: await interaction.response.send_message(embed=discord.Embed(title=f"Allowed Channels for {interaction.guild.name}", description='\n'.join(f"- <#{ch_id}> (`{ch_id}`)" for ch_id in channel_list), color=discord.Color.blue()), ephemeral=True)
    else: await interaction.response.send_message("ℹ️ Monitoring all channels.", ephemeral=True)

@bot.slash_command(name="config_channel_add", description="Adds a channel to the allowed list.")
async def config_channel_add(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "The text channel to allow monitoring in.")):
    """Adds a channel to the allowed list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    allowed_channels = guild_config.get('allowed_channel_ids', [])
    if allowed_channels is None: allowed_channels = [] # Ensure it's a list if currently None

    if channel_id not in allowed_channels:
        allowed_channels.append(channel_id)
        guild_config['allowed_channel_ids'] = allowed_channels # Update the list in the config copy
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Added {channel.mention} to monitored channels.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save config after adding channel.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {channel.mention} is already being monitored.", ephemeral=True)

@bot.slash_command(name="config_channel_remove", description="Removes a channel from the allowed list.")
async def config_channel_remove(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "The text channel to stop monitoring.")):
    """Removes a channel from the allowed list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    allowed_channels = guild_config.get('allowed_channel_ids')

    if allowed_channels and channel_id in allowed_channels:
        allowed_channels.remove(channel_id);
        # If list becomes empty, set back to None to monitor all
        guild_config['allowed_channel_ids'] = allowed_channels if allowed_channels else None
        if await save_guild_config(guild_id, guild_config):
            msg = f"✅ Removed {channel.mention}."
            if guild_config['allowed_channel_ids'] is None:
                 msg += " Now monitoring all channels."
            await interaction.response.send_message(msg, ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save config after removing channel.", ephemeral=True)
    elif allowed_channels is None:
         await interaction.response.send_message(f"ℹ️ Currently monitoring all channels. Cannot remove {channel.mention} specifically.", ephemeral=True)
    else: # List exists but channel isn't in it
         await interaction.response.send_message(f"ℹ️ {channel.mention} was not in the monitored channel list.", ephemeral=True)

@bot.slash_command(name="config_channel_clear", description="Clears the allowed channel list (monitors all channels).")
async def config_channel_clear(interaction: discord.Interaction):
    """Clears the allowed channel list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy()
    if guild_config.get('allowed_channel_ids') is not None:
        guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message("✅ Cleared specific channel monitoring. Now monitoring all channels.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save config after clearing channel list.", ephemeral=True)
    else: await interaction.response.send_message("ℹ️ Already monitoring all channels.", ephemeral=True)

# --- Allowlist Commands (Now Top-Level) ---

@bot.slash_command(name="allowlist_view", description="Shows the current user allowlist for this server.")
async def allowlist_view(interaction: discord.Interaction):
    """Displays the allowlisted users."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id); user_list = guild_config.get('allowed_users', [])
    if user_list:
        embed = discord.Embed(title=f"Allowlisted Users for {interaction.guild.name}", color=discord.Color.green()); mentions = []
        for user_id in user_list:
            try:
                # Try fetching from cache first, then API
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                if user: mentions.append(f"- {user.mention} (`{user_id}`)")
                else: mentions.append(f"- *Unknown User* (`{user_id}`)") # Should not happen if fetch_user works
            except discord.NotFound:
                 mentions.append(f"- *Unknown User (ID not found)* (`{user_id}`)")
            except Exception as e:
                 mentions.append(f"- *Error fetching user* (`{user_id}`)")
                 print(f"Error fetching user {user_id} for allowlist view: {e}")
        embed.description = '\n'.join(mentions); await interaction.response.send_message(embed=embed, ephemeral=True)
    else: await interaction.response.send_message("ℹ️ No users currently allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_add", description="Adds a user to the allowlist (exempt from checks).")
async def allowlist_add(interaction: discord.Interaction, user: discord.Option(discord.User, "The user to allowlist.")):
    """Adds a user to the allowlist."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    allowed_users = guild_config.get('allowed_users', [])
    if allowed_users is None: allowed_users = [] # Ensure list

    if user_id not in allowed_users:
        allowed_users.append(user_id)
        guild_config['allowed_users'] = allowed_users
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Added {user.mention} to allowlist.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save config after adding user to allowlist.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {user.mention} is already allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_remove", description="Removes a user from the allowlist.")
async def allowlist_remove(interaction: discord.Interaction, user: discord.Option(discord.User, "The user to remove from the allowlist.")):
    """Removes a user from the allowlist."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    allowed_users = guild_config.get('allowed_users')

    if allowed_users and user_id in allowed_users:
        allowed_users.remove(user_id)
        guild_config['allowed_users'] = allowed_users # Save updated list
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Removed {user.mention} from allowlist.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save config after removing user from allowlist.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {user.mention} was not found in the allowlist.", ephemeral=True)


# --- Hash Management Commands (Now Top-Level) ---

def parse_message_id(message_ref: str) -> int | None:
    """Extracts message ID from link or plain ID."""
    match = re.search(r'/(\d+)$', message_ref.strip());
    if match:
        try: return int(match.group(1))
        except ValueError: return None
    elif message_ref.strip().isdigit():
        try: return int(message_ref.strip())
        except ValueError: return None
    return None

@bot.slash_command(name="hash_remove", description="Removes the stored hash associated with a specific message ID or link.")
async def remove_hash(interaction: discord.Interaction, message_reference: discord.Option(str, "The message ID or link containing the image hash to remove.")):
    """Removes a hash entry based on the original message ID."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()

    target_message_id = parse_message_id(message_reference)
    if target_message_id is None: await interaction.response.send_message("❌ Invalid message ID or link format. Please provide a valid message ID or a direct message link.", ephemeral=True); return

    target_message_id_str = str(target_message_id); stored_hashes = await load_guild_hashes(guild_id, loop)
    hash_removed = False; keys_removed = [] # Track all keys removed for that message ID
    target_channel_key = None # For channel scope

    if not isinstance(stored_hashes, dict): await interaction.response.send_message("ℹ️ Hash database is empty or invalid.", ephemeral=True); return

    # --- Find and remove logic ---
    if current_scope == "server":
        keys_to_remove_now = [identifier for identifier in stored_hashes if identifier.startswith(target_message_id_str + "-")]
        for key in keys_to_remove_now:
            del stored_hashes[key]; keys_removed.append(key); hash_removed = True
    elif current_scope == "channel":
        for ch_id_str, channel_hashes in stored_hashes.items():
            if isinstance(channel_hashes, dict):
                keys_to_remove_now = [identifier for identifier in channel_hashes if identifier.startswith(target_message_id_str + "-")]
                if keys_to_remove_now: target_channel_key = ch_id_str
                for key in keys_to_remove_now:
                    del channel_hashes[key]; keys_removed.append(key); hash_removed = True
        # Clean up empty channel entry if needed
        if target_channel_key and not stored_hashes[target_channel_key]:
             del stored_hashes[target_channel_key]

    # --- Save and respond ---
    if hash_removed:
        if await save_guild_hashes(guild_id, stored_hashes, loop):
             await interaction.response.send_message(f"✅ Removed {len(keys_removed)} hash entr(y/ies) associated with message ID `{target_message_id}` (Keys: `{', '.join(keys_removed)}`).", ephemeral=True)
        else: await interaction.response.send_message("⚠️ Error saving hash database after removal.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ No hash entries found starting with message ID `{target_message_id}`.", ephemeral=True)

@bot.slash_command(name="hash_clear", description="Clears hashes for the server or a channel. Requires confirmation!")
async def clear_hashes(
    interaction: discord.Interaction,
    confirm: discord.Option(bool, "Must be True to confirm deletion.", required=True),
    channel: discord.Option(discord.TextChannel, "Optional: Specify channel to clear only its hashes (if scope='channel').", required=False, default=None)
):
    """Clears hashes with confirmation."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 **Confirmation required!** Set `confirm:True` to proceed.", ephemeral=True); return

    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()
    target_desc = f"channel {channel.mention}" if channel else "the entire server"

    await interaction.response.defer(ephemeral=True) # Defer response while loading/saving

    stored_hashes = await load_guild_hashes(guild_id, loop); cleared = False; original_count = 0

    if not isinstance(stored_hashes, dict): await interaction.followup.send(f"ℹ️ Hash database empty/invalid for {target_desc}.", ephemeral=True); return

    if channel: # Clear specific channel
        target_channel_id_str = str(channel.id)
        if current_scope == "channel":
            if target_channel_id_str in stored_hashes:
                 original_count = len(stored_hashes[target_channel_id_str])
                 del stored_hashes[target_channel_id_str]; cleared = True
            else: await interaction.followup.send(f"ℹ️ No hashes found stored specifically for {channel.mention} (Scope is 'channel').", ephemeral=True); return
        elif current_scope == "server":
            await interaction.followup.send(f"ℹ️ Cannot clear specific channel when scope is 'server'. Use `/hash_clear confirm:True` without specifying a channel to clear all server hashes.", ephemeral=True); return
    else: # Clear all for guild
        if current_scope == "server":
            original_count = len(stored_hashes)
        elif current_scope == "channel":
            original_count = sum(len(v) for v in stored_hashes.values() if isinstance(v, dict))
        stored_hashes.clear(); cleared = True

    # Save and respond
    if cleared:
        if await save_guild_hashes(guild_id, stored_hashes, loop):
             await interaction.followup.send(f"✅ Cleared {original_count} hash entr(y/ies) for {target_desc}.", ephemeral=True)
        else: await interaction.followup.send(f"⚠️ Error saving cleared hash database.", ephemeral=True)
    # No else needed as specific cases are handled above


# --- Scan Command (With Logging Option) ---
@bot.slash_command(name="scan", description="Scans channel history to add/update image hashes.")
async def scan_history(
    interaction: discord.Interaction,
    channel: discord.Option(discord.TextChannel, "The channel to scan."),
    limit: discord.Option(int, "Max number of messages to scan.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT),
    flag_duplicates: discord.Option(bool, "Add reaction to non-oldest duplicates found?", default=False),
    reply_to_duplicates: discord.Option(bool, "Reply to non-oldest duplicates found?", default=False),
    delete_duplicates: discord.Option(bool, "Delete non-oldest duplicates found? (Requires Manage Messages)", default=False),
    log_scan_duplicates: discord.Option(bool, "Log duplicates found during scan to the log channel?", default=False) # <-- New Option
):
    """Scans past messages, populates DB (finds oldest), optionally flags/replies/deletes/logs non-oldest duplicates."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild context.", ephemeral=True); return

    guild_id = interaction.guild_id; scan_channel_id = channel.id; scan_channel_id_str = str(scan_channel_id)
    guild_config = get_guild_config(guild_id); current_scope = guild_config.get('duplicate_scope', 'server')
    current_hash_size = guild_config.get('hash_size', 8); existence_threshold = 0 # Use 0 for exact match check during scan update
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE)
    reply_on_scan_duplicate_config = guild_config.get('reply_on_duplicate', True) # Check if global replies are enabled
    log_channel_id = guild_config.get('log_channel_id')

    # Check permissions
    bot_member = interaction.guild.me
    if not channel.permissions_for(bot_member).read_message_history:
         await interaction.response.send_message(f"❌ Bot lacks 'Read Message History' in {channel.mention}.", ephemeral=True); return
    perms_needed = []
    if flag_duplicates and not channel.permissions_for(bot_member).add_reactions: perms_needed.append("Add Reactions")
    # Check send messages perm only if BOTH global reply setting AND scan reply option are true
    if reply_on_scan_duplicate_config and reply_to_duplicates and not channel.permissions_for(bot_member).send_messages: perms_needed.append("Send Messages")
    if delete_duplicates and not channel.permissions_for(bot_member).manage_messages: perms_needed.append("Manage Messages")
    if log_scan_duplicates and not log_channel_id:
        await interaction.response.send_message(f"❌ Logging requested, but no log channel is configured. Use `/config_set_log_channel` first.", ephemeral=True); return
    # Check log channel perms if logging is requested
    if log_scan_duplicates and log_channel_id:
        log_channel = interaction.guild.get_channel(log_channel_id)
        if not log_channel or not isinstance(log_channel, discord.TextChannel):
            perms_needed.append(f"Access/View Log Channel ({log_channel_id})")
        elif not log_channel.permissions_for(bot_member).send_messages or not log_channel.permissions_for(bot_member).embed_links:
             perms_needed.append(f"Send/Embed in Log Channel ({log_channel.mention})")


    if perms_needed:
        await interaction.response.send_message(f"❌ Bot lacks permissions ({', '.join(perms_needed)}) for requested actions.", ephemeral=True); return

    await interaction.response.defer(ephemeral=False) # Defer publicly
    status_message = await interaction.followup.send(
        f"⏳ Starting scan: {channel.mention}, limit={limit}, "
        f"Flagging:{flag_duplicates}, Replying:{reply_to_duplicates}, Deleting:{delete_duplicates}, Logging:{log_scan_duplicates}...",
        wait=True
    )

    processed_messages = 0; added_hashes = 0; updated_hashes = 0; flagged_count = 0; replied_count = 0; deleted_count = 0; logged_count = 0 # <-- New counter
    skipped_attachments = 0; errors = 0
    loop = asyncio.get_running_loop()

    # --- Phase 1: Gather all image data ---
    scanned_image_data = defaultdict(list) # {hash_str: [(timestamp, msg_id, user_id, filename, msg_obj), ...]}
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1: Gathering image data from {channel.name}...")
    start_time_phase1 = datetime.datetime.now()
    try:
        async for message in channel.history(limit=limit):
            processed_messages += 1
            if processed_messages % SCAN_UPDATE_INTERVAL == 0:
                try: await status_message.edit(content=f"⏳ Scanning... Processed {processed_messages}/{limit} messages (Phase 1).")
                except Exception as e: print(f"DEBUG: Error editing status (Phase 1 Loop): {e}") # Log error but continue
            if message.author.bot or not message.attachments: continue

            message_user_id = message.author.id
            message_timestamp = message.created_at.replace(tzinfo=datetime.timezone.utc) # Ensure UTC

            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    try:
                        image_bytes = await attachment.read();
                        img_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                        if img_hash is None: skipped_attachments += 1; continue

                        # Store message object directly for later use in actions
                        scanned_image_data[str(img_hash)].append((message_timestamp, message.id, message_user_id, attachment.filename, message))
                        # print(f"DEBUG: [Scan G:{guild_id}] Found hash {img_hash} for msg {message.id}") # Very verbose debug
                    except discord.HTTPException as e: print(f"Warning: [Scan G:{guild_id}] HTTP Failed download attach {attachment.id} from msg {message.id}: {e.status} {e.code}"); errors += 1; skipped_attachments += 1
                    except UnidentifiedImageError: print(f"Warning: [Scan G:{guild_id}] Skipping unidentifiable image attach {attachment.id} from msg {message.id}"); skipped_attachments += 1 # Don't count as error if PIL just can't read it
                    except Exception as e: print(f"Error: [Scan G:{guild_id}] Error processing attach {attachment.id} from msg {message.id}: {e}"); errors += 1; skipped_attachments += 1; traceback.print_exc()
    except discord.Forbidden:
         final_content = f"❌ Scan failed (Phase 1). Bot lacks Read Message History permission in {channel.mention}."
         try: await status_message.edit(content=final_content)
         except discord.HTTPException: await interaction.channel.send(content=final_content) # Fallback
         return
    except Exception as e:
         final_content = f"❌ Error during scan (Phase 1): {e}"
         try: await status_message.edit(content=final_content)
         except discord.HTTPException: await interaction.channel.send(content=final_content) # Fallback
         traceback.print_exc(); return

    elapsed_phase1 = (datetime.datetime.now() - start_time_phase1).total_seconds()
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1 complete ({elapsed_phase1:.2f}s). Found {len(scanned_image_data)} unique hash groups from {processed_messages} messages.")

    # --- Phase 2: Process hashes, update DB, perform actions ---
    start_time_phase2 = datetime.datetime.now()
    try: await status_message.edit(content=f"⏳ Processing {len(scanned_image_data)} unique hash groups...")
    except Exception as e: print(f"DEBUG: Error editing status (Phase 2 Start): {e}")

    stored_hashes = await load_guild_hashes(guild_id, loop); db_updated = False; processed_hashes = 0
    for img_hash_str, entries in scanned_image_data.items():
        processed_hashes += 1
        # Update status less frequently during processing which might be faster per item
        if processed_hashes % (SCAN_UPDATE_INTERVAL * 2) == 0:
            status_text = (f"⏳ Processing... {processed_hashes}/{len(scanned_image_data)} hashes. "
                           f"Added:{added_hashes} Updated:{updated_hashes} Replied:{replied_count} "
                           f"Flagged:{flagged_count} Deleted:{deleted_count} Logged:{logged_count}") # <-- Add logged counter
            try: await status_message.edit(content=status_text)
            except Exception as e: print(f"DEBUG: Error editing status (Phase 2 Loop): {e}")

        if not entries: continue # Should not happen with defaultdict, but safety check

        # Sort entries by timestamp to find the oldest
        entries.sort(key=lambda x: x[0]); oldest_entry = entries[0]
        oldest_timestamp, oldest_msg_id, oldest_user_id, oldest_filename, oldest_message_obj = oldest_entry
        oldest_identifier = f"{oldest_msg_id}-{oldest_filename}"

        # Only create hash object once per group
        try: img_hash_obj = imagehash.hex_to_hash(img_hash_str)
        except ValueError: print(f"Warning: [Scan G:{guild_id}] Invalid hash string '{img_hash_str}' from scan phase, skipping group."); errors += len(entries); continue

        # Check if this exact hash already exists in the DB
        existing_identifier, existing_data = find_existing_hash_entry_sync(img_hash_obj, stored_hashes, existence_threshold, current_scope, scan_channel_id_str)

        # Determine if DB needs update and which entry is canonical (oldest)
        update_needed = False; identifier_to_use = oldest_identifier
        data_to_use = {"hash": img_hash_str, "user_id": oldest_user_id, "timestamp": oldest_timestamp.isoformat()}
        existing_time = None

        if existing_identifier:
            # If existing entry is found, compare timestamps (if possible)
            existing_timestamp_str = None
            if isinstance(existing_data, dict): existing_timestamp_str = existing_data.get('timestamp')

            if existing_timestamp_str:
                try:
                    existing_time = dateutil.parser.isoparse(existing_timestamp_str)
                    if existing_time.tzinfo is None: existing_time = existing_time.replace(tzinfo=datetime.timezone.utc)

                    if oldest_timestamp < existing_time:
                        update_needed = True # Scanned message is older, replace DB entry
                        # Keep data_to_use as the new oldest
                    else:
                        # DB entry is older or same age, keep it as canonical
                        identifier_to_use = existing_identifier
                        data_to_use = existing_data # Keep the full existing data dict
                        update_needed = False # DB doesn't need changing for this hash's primary record
                except Exception as parse_e:
                    # Error parsing DB timestamp, assume scanned is better
                    print(f"Warning: [Scan G:{guild_id}] Error parsing DB timestamp '{existing_timestamp_str}' for ID {existing_identifier}: {parse_e}. Assuming update needed.")
                    update_needed = True
            else: # Existing entry has no timestamp (old format?) - update it
                update_needed = True
        else: # Hash not found in DB at all
            update_needed = True

        # --- Update Hash Database if needed ---
        if update_needed:
            db_updated = True
            if existing_identifier and oldest_timestamp < (existing_time or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)): # Check existing_time if available
                updated_hashes += 1
            elif not existing_identifier:
                added_hashes += 1

            # Add/Update logic based on scope
            if current_scope == "server":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                # Remove old entry only if the identifier changed (we found an older one)
                if existing_identifier and existing_identifier != identifier_to_use:
                    stored_hashes.pop(existing_identifier, None)
                stored_hashes[identifier_to_use] = data_to_use
            elif current_scope == "channel":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                channel_hashes = stored_hashes.setdefault(scan_channel_id_str, {})
                if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[scan_channel_id_str] = channel_hashes
                # Remove old entry only if the identifier changed
                if existing_identifier and existing_identifier != identifier_to_use:
                    channel_hashes.pop(existing_identifier, None)
                channel_hashes[identifier_to_use] = data_to_use

        # --- Process non-oldest entries in the group for actions ---
        # Use the canonical oldest info (either from scan or pre-existing DB entry)
        canonical_oldest_id = identifier_to_use
        canonical_oldest_user_id = data_to_use.get('user_id') # Get user_id from the data we decided to use
        canonical_oldest_msg_obj = oldest_message_obj if identifier_to_use == oldest_identifier else None # Only have msg obj if scan found oldest

        for entry_timestamp, entry_msg_id, entry_user_id, entry_filename, entry_message_obj in entries:
            # Skip the entry identified as the canonical oldest
            if entry_msg_id == int(canonical_oldest_id.split('-')[0]): # Compare msg IDs
                 continue

            # Determine if the current entry is a violation based on mode and canonical oldest user ID
            is_violation = False
            if current_mode == "strict": is_violation = True
            elif current_mode == "owner_allowed":
                if canonical_oldest_user_id is None or canonical_oldest_user_id != entry_user_id: is_violation = True

            # Check duration if applicable (compare entry time to canonical oldest time)
            if is_violation and current_duration > 0:
                try:
                     canonical_oldest_time_str = data_to_use.get('timestamp')
                     if canonical_oldest_time_str:
                         canonical_oldest_time = dateutil.parser.isoparse(canonical_oldest_time_str)
                         if canonical_oldest_time.tzinfo is None: canonical_oldest_time = canonical_oldest_time.replace(tzinfo=datetime.timezone.utc)
                         if (entry_timestamp - canonical_oldest_time).days > current_duration: is_violation = False
                     else: # Cannot check duration if oldest has no timestamp
                         is_violation = False
                except Exception: is_violation = False # Error parsing time, assume not violation


            # Perform actions if it's a violation
            if is_violation:
                action_taken = False

                # --- <<<< LOG DUPLICATE (New) >>>> ---
                if log_scan_duplicates and interaction.guild:
                    log_embed = discord.Embed(
                        title="Duplicate Image Detected (Scan)",
                        color=discord.Color.orange(),
                        timestamp=datetime.datetime.now(datetime.timezone.utc) # Log time is now
                    )
                    log_embed.add_field(name="Duplicate User", value=f"<@{entry_user_id}> (`{entry_user_id}`)", inline=True)
                    log_embed.add_field(name="Channel", value=channel.mention, inline=True) # Channel being scanned
                    try:
                         log_embed.add_field(name="Duplicate Msg", value=f"[Jump Link]({entry_message_obj.jump_url})", inline=True)
                    except: log_embed.add_field(name="Duplicate Msg", value=f"`{entry_msg_id}`", inline=True)

                    log_embed.add_field(name="Image Hash", value=f"`{img_hash_str}`", inline=False)
                    log_embed.add_field(name="Match Identifier", value=f"`{canonical_oldest_id}`", inline=True) # ID of the oldest match
                    log_embed.add_field(name="Hash Distance", value="0 (Exact Scan Match)", inline=True)
                    orig_user_mention = f"<@{canonical_oldest_user_id}> (`{canonical_oldest_user_id}`)" if canonical_oldest_user_id else "Unknown"
                    log_embed.add_field(name="Original User", value=orig_user_mention, inline=True)

                    # Add link to original message if possible
                    original_msg_link = None
                    try: # Try getting jump url from oldest obj if we have it, else build approx link
                        if canonical_oldest_msg_obj:
                            original_msg_link = canonical_oldest_msg_obj.jump_url
                        elif canonical_oldest_id:
                            orig_msg_id_int = int(canonical_oldest_id.split('-')[0])
                            # Assume same channel for link if we don't have the object
                            original_msg_link = f"https://discord.com/channels/{guild_id}/{scan_channel_id}/{orig_msg_id_int}"
                    except: pass # Ignore errors building link

                    if original_msg_link:
                         log_embed.add_field(name="Original Message", value=f"[Approx. Link]({original_msg_link})", inline=False)

                    # Add thumbnail of the *duplicate* image
                    thumb_url = None
                    if entry_message_obj and entry_message_obj.attachments:
                        # Find the attachment corresponding to this hash - usually the first image
                         for att in entry_message_obj.attachments:
                             if att.content_type and att.content_type.startswith('image/'):
                                 thumb_url = att.url
                                 break
                    if thumb_url: log_embed.set_thumbnail(url=thumb_url)

                    log_embed.set_footer(text=f"Scan initiated by: {interaction.user.name} | Guild ID: {guild_id}")
                    await log_event(interaction.guild, log_embed)
                    logged_count += 1 # Increment log counter
                    action_taken = True # Logging counts as an action for delay purposes

                # --- Reply (only if global reply is on AND scan reply option is on) ---
                if reply_on_scan_duplicate_config and reply_to_duplicates:
                    try:
                        template_data = {
                            "mention": f"<@{entry_user_id}>",
                            "filename": entry_filename,
                            "identifier": canonical_oldest_id, # Use the identifier of the actual oldest
                            "distance": 0, # Distance is 0 for exact hash match in scan
                            "original_user_mention": f"<@{canonical_oldest_user_id}>" if canonical_oldest_user_id else "*Unknown*",
                            "emoji": duplicate_reaction_emoji,
                            "original_user_info": f", Orig User: <@{canonical_oldest_user_id}>" if canonical_oldest_user_id else "",
                            "jump_link": ""
                        }
                        # Try adding jump link to oldest
                        if canonical_oldest_msg_obj:
                            try: template_data["jump_link"] = f"\nOriginal: {canonical_oldest_msg_obj.jump_url}"
                            except: pass
                        elif canonical_oldest_id: # Build approx link if no object
                            try:
                                orig_msg_id_int = int(canonical_oldest_id.split('-')[0])
                                template_data["jump_link"] = f"\nOriginal: https://discord.com/channels/{guild_id}/{scan_channel_id}/{orig_msg_id_int}"
                            except: pass

                        reply_text = reply_template.format_map(defaultdict(str, template_data))
                        await entry_message_obj.reply(reply_text, mention_author=False) # Don't ping during scan
                        replied_count += 1; action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Send Messages perm for reply to {entry_msg_id}."); break # Stop actions
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for reply.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed reply to {entry_msg_id}: {e}")

                # --- React ---
                if flag_duplicates:
                    try:
                        refreshed_message = await channel.fetch_message(entry_message_obj.id)
                        already_reacted = False;
                        for reaction in refreshed_message.reactions:
                            if str(reaction.emoji) == duplicate_reaction_emoji and reaction.me:
                                already_reacted = True; break
                        if not already_reacted:
                             await entry_message_obj.add_reaction(duplicate_reaction_emoji)
                             flagged_count += 1; action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Add Reactions perm for reaction on {entry_msg_id}."); break # Stop actions
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for reaction.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed reaction on {entry_msg_id}: {e}")

                # --- Delete ---
                if delete_duplicates:
                    try:
                        await entry_message_obj.delete()
                        deleted_count += 1; action_taken = True
                        print(f"DEBUG: [Scan Action G:{guild_id}] Deleted msg {entry_msg_id}")
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Manage Messages perm for delete on {entry_msg_id}."); break # Stop actions
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for deletion.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed delete msg {entry_msg_id}: {e}")

                # Delay if any action was attempted/succeeded
                if action_taken: await asyncio.sleep(SCAN_ACTION_DELAY)

    # --- Final Save and Report ---
    if db_updated:
        print(f"DEBUG: [Scan G:{guild_id}] Saving updated hash database after scan...")
        if not await save_guild_hashes(guild_id, stored_hashes, loop):
            print(f"ERROR: [Scan G:{guild_id}] CRITICAL: Failed to save hash database after scan modifications!")
            # Try to notify about save error via interaction channel if possible
            try:
                 await interaction.followup.send("⚠️ CRITICAL: Error saving hashes after scan! Data integrity might be compromised.", ephemeral=True)
            except discord.HTTPException: # Token might be expired here too
                 print(f"ERROR: [Scan G:{guild_id}] Failed to save hashes AND failed to send followup warning.")
                 # Optionally send to log channel if configured and possible
                 # await log_event(...)

    elapsed_phase2 = (datetime.datetime.now() - start_time_phase2).total_seconds()
    total_elapsed = (datetime.datetime.now() - start_time_phase1).total_seconds()
    print(f"DEBUG: [Scan G:{guild_id}] Phase 2 complete ({elapsed_phase2:.2f}s). Total scan time: {total_elapsed:.2f}s.")

    # --- Final Status Update (With Fallback) ---
    final_message_content = (
        f"✅ Scan Complete! Processed {processed_messages} messages ({total_elapsed:.2f}s). "
        f"Added:{added_hashes}, Updated:{updated_hashes}, Replied:{replied_count}, "
        f"Flagged:{flagged_count}, Deleted:{deleted_count}, Logged:{logged_count}. " # <-- Add logged count
        f"Skipped:{skipped_attachments}. Errors:{errors}."
    )

    try:
        # Try editing the original status message first
        await status_message.edit(content=final_message_content)
        print(f"DEBUG: [Scan G:{guild_id}] Successfully edited final status message.")
    except discord.HTTPException as e:
        # If editing fails (likely due to expired token 50027 or general 401), send a new message
        print(f"DEBUG: [Scan G:{guild_id}] Editing status message failed (Status: {e.status}, Code: {e.code}, Text: '{e.text}'). Attempting to send new message.")
        if e.code == 50027 or e.status == 401:
            try:
                # Attempt to delete the "Scanning..." message first (might also fail)
                try:
                    await status_message.delete()
                    print(f"DEBUG: [Scan G:{guild_id}] Deleted original 'Scanning...' status message.")
                    await asyncio.sleep(0.5) # Small delay before sending new
                except Exception as del_e:
                    print(f"DEBUG: [Scan G:{guild_id}] Failed to delete original 'Scanning...' status message: {del_e}")

                # Send the final status as a new message in the original channel
                if interaction.channel:
                     await interaction.channel.send(content=final_message_content)
                     print(f"DEBUG: [Scan G:{guild_id}] Sent final status as a new message.")
                else:
                     print(f"ERROR: [Scan G:{guild_id}] interaction.channel is None, cannot send final status message.")
            except discord.Forbidden:
                 print(f"ERROR: [Scan G:{guild_id}] Missing permissions to send messages or delete in {channel.mention}.")
            except Exception as send_e:
                 print(f"ERROR: [Scan G:{guild_id}] Failed to send final status as new message: {send_e}")
                 traceback.print_exc()
        else:
            # Handle other potential HTTP errors during edit
            print(f"ERROR: [Scan G:{guild_id}] An unexpected HTTP error occurred during final status edit: {e}")
            traceback.print_exc()
    except Exception as final_edit_err:
         print(f"ERROR: [Scan G:{guild_id}] A non-HTTP error occurred during final status edit attempt: {final_edit_err}")
         traceback.print_exc()
         # Best effort fallback to send new message
         try:
             if interaction.channel: await interaction.channel.send(content=final_message_content)
         except Exception: pass


# --- New Clear Flags Command ---
@bot.slash_command(name="clearflags", description="Removes the bot's duplicate warning reactions from messages in a channel.")
async def clear_flags(
    interaction: discord.Interaction,
    channel: discord.Option(discord.TextChannel, "The channel to clear reactions from."),
    confirm: discord.Option(bool, "Must be True to confirm clearing reactions.", required=True),
    limit: discord.Option(int, "Max number of messages to check.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT)
):
    """Removes the bot's configured duplicate reaction from messages in channel history."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 **Confirmation required!** Set `confirm:True` to proceed.", ephemeral=True); return
    if not interaction.guild: await interaction.response.send_message("❌ Cannot determine guild context.", ephemeral=True); return

    guild_id = interaction.guild_id
    guild_config = get_guild_config(guild_id)
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    bot_member = interaction.guild.me

    # Check permissions
    if not channel.permissions_for(bot_member).read_message_history:
         await interaction.response.send_message(f"❌ Bot lacks 'Read Message History' in {channel.mention}.", ephemeral=True); return
    # Need Add Reactions to remove own reactions reliably
    if not channel.permissions_for(bot_member).add_reactions:
         await interaction.response.send_message(f"❌ Bot lacks 'Add Reactions' permission in {channel.mention} (needed to remove own reactions).", ephemeral=True); return


    await interaction.response.defer(ephemeral=False) # Defer publicly
    status_message = await interaction.followup.send(f"⏳ Starting reaction cleanup ({duplicate_reaction_emoji}) in {channel.mention} (limit {limit})...", wait=True)

    processed_msgs = 0; reactions_removed = 0; errors = 0
    final_status_content = "" # Define outside try/finally
    start_time = datetime.datetime.now()
    try:
        async for message in channel.history(limit=limit):
            processed_msgs += 1
            if processed_msgs % (SCAN_UPDATE_INTERVAL * 2) == 0: # Update less often for clearflags
                try: await status_message.edit(content=f"⏳ Clearing flags... Checked {processed_msgs}/{limit}. Removed {reactions_removed}.")
                except Exception as e: print(f"DEBUG: Error editing status (ClearFlags): {e}")

            # Check reactions on the message
            reaction_to_remove = None
            for reaction in message.reactions:
                 if str(reaction.emoji) == duplicate_reaction_emoji:
                     if reaction.me: # Found the bot's reaction
                         reaction_to_remove = reaction
                         break

            if reaction_to_remove:
                 try:
                     await message.remove_reaction(reaction_to_remove.emoji, bot_member)
                     reactions_removed += 1
                     await asyncio.sleep(CLEAR_REACTION_DELAY)
                 except discord.Forbidden:
                     print(f"Warning: [ClearFlags G:{guild_id}] Missing permission to remove reaction in {channel.mention} (Msg ID: {message.id}). Stopping.")
                     errors += 1
                     final_status_content = f"⚠️ Cleanup stopped due to permission error in {channel.mention}. Checked {processed_msgs}. Removed {reactions_removed}."
                     break # Stop processing
                 except discord.NotFound: pass # Message or reaction already gone, ignore
                 except Exception as e:
                     print(f"Error: [ClearFlags G:{guild_id}] Failed to remove reaction from {message.id}: {e}")
                     errors += 1

        # If loop completed without breaking
        if not final_status_content:
            elapsed = (datetime.datetime.now() - start_time).total_seconds()
            final_status_content = (f"✅ Cleanup Complete! Checked {processed_msgs} messages in {channel.mention} ({elapsed:.2f}s). "
                                    f"Removed **{reactions_removed}** '{duplicate_reaction_emoji}' reactions. Errors: {errors}.")

    except discord.Forbidden:
        final_status_content = f"❌ Cleanup failed. Bot lacks Read Message History permission in {channel.mention}."
        errors += 1
    except Exception as e:
        final_status_content = f"❌ Error during cleanup: {e}"
        traceback.print_exc()
        errors += 1
    finally:
        # --- Final status update for clearflags (using fallback) ---
        try:
             await status_message.edit(content=final_status_content)
        except discord.HTTPException as e_http:
             print(f"DEBUG: [ClearFlags G:{guild_id}] Editing final status failed (Status: {e_http.status}, Code: {e_http.code}). Sending new message.")
             if e_http.code == 50027 or e_http.status == 401:
                 try:
                     await status_message.delete()
                 except Exception: pass
                 try:
                     if interaction.channel: await interaction.channel.send(content=final_status_content)
                 except Exception as e_send: print(f"ERROR: [ClearFlags G:{guild_id}] Failed to send final status as new message: {e_send}")
             else:
                 print(f"ERROR: [ClearFlags G:{guild_id}] An unexpected HTTP error occurred during final status edit: {e_http}")
        except Exception as e_final:
             print(f"ERROR: [ClearFlags G:{guild_id}] A non-HTTP error occurred during final status edit: {e_final}")
             try: # Best effort fallback
                 if interaction.channel: await interaction.channel.send(content=final_status_content)
             except Exception: pass


# --- Main Execution ---
if __name__ == "__main__":
    try: import dateutil.parser
    except ImportError: print("Optional dependency 'python-dateutil' not found. Timestamps might not parse correctly. Consider: pip install python-dateutil")
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found in environment variables or .env file.", file=sys.stderr); sys.exit(1)
    try: print("Starting bot..."); bot.run(BOT_TOKEN)
    except discord.LoginFailure: print("Error: Improper token passed. Ensure DISCORD_BOT_TOKEN is correct.", file=sys.stderr)
    except discord.PrivilegedIntentsRequired: print("Error: Privileged Intents (Message Content) are not enabled. Go to the Discord Developer Portal -> Your App -> Bot -> Enable Message Content Intent.", file=sys.stderr)
    except Exception as e: print(f"An error occurred while starting or running the bot: {e}", file=sys.stderr); traceback.print_exc()
    finally: print("--- Bot process ended. ---")

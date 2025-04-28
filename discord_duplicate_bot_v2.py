#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate images with per-server configuration.
Supports scope, check mode, time limits, history scanning (!scan),
hash removal (!removehash), hash clearing (!clearhashes),
and user allowlisting (!allowuser, !unallowuser, !allowlist).
Loads token from .env, settings from server_configs.json via commands.
"""

import discord
from discord.ext import commands
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

# --- Constants ---
CONFIG_FILE_PATH = 'server_configs.json'
DEFAULT_COMMAND_PREFIX = "!"
HASH_FILENAME_PREFIX = "hashes_"
VALID_SCOPES = ["server", "channel"]
VALID_CHECK_MODES = ["strict", "owner_allowed"]
DEFAULT_SCAN_LIMIT = 1000 # Default message limit for !scan
SCAN_UPDATE_INTERVAL = 100 # How often to update scan progress message

# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# --- Global Configuration Cache & Locks ---
server_configs = {}
config_lock = asyncio.Lock()
hash_file_locks = {}

# --- Configuration Loading/Saving ---

def get_default_guild_config(guild_id):
    """Returns default settings, including allowlist."""
    return {
        "hash_db_file": f"{HASH_FILENAME_PREFIX}{guild_id}.json",
        "hash_size": 8,
        "similarity_threshold": 5,
        "allowed_channel_ids": None,
        "react_to_duplicates": True,
        "delete_duplicates": False,
        "duplicate_reaction_emoji": "⚠️",
        "duplicate_scope": "server",
        "duplicate_check_mode": "strict",
        "duplicate_check_duration_days": 0, # 0 = check forever
        "allowed_users": [] # List of user IDs exempt from checks
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
    except: return None
async def calculate_hash(image_bytes, hash_size, loop):
    func = partial(calculate_hash_sync, image_bytes, hash_size); return await loop.run_in_executor(None, func)

def load_hashes_sync(db_file):
    if not os.path.exists(db_file): return {}
    try:
        with open(db_file, 'r', encoding='utf-8') as f: data = json.load(f)
        if not isinstance(data, dict): return {}
        # Format check remains the same
        is_new_format_likely = False; has_timestamp = False
        if data:
            for v in data.values():
                if isinstance(v, dict) and 'hash' in v and 'user_id' in v:
                    is_new_format_likely = True
                    if 'timestamp' in v: has_timestamp = True; break
                if isinstance(v, dict):
                    for subv in v.values():
                        if isinstance(subv, dict) and 'hash' in subv and 'user_id' in subv:
                            is_new_format_likely = True
                            if 'timestamp' in subv: has_timestamp = True; break
                if is_new_format_likely: break
        if is_new_format_likely and not has_timestamp and data: print(f"Warning: Hash file '{db_file}' missing timestamps.", file=sys.stderr)
        elif not is_new_format_likely and data: print(f"Warning: Hash file '{db_file}' seems old format.", file=sys.stderr)
        return data
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

# --- Duplicate Finding (Scope Aware, Time Aware, Returns UserID) ---
def find_hash_exists_sync(target_hash, stored_hashes_dict, threshold, scope, channel_id_str):
    """Checks if a similar hash exists, respecting scope."""
    if target_hash is None: return False
    hashes_to_check = {}
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return False
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        if isinstance(hash_data, dict) and 'hash' in hash_data: stored_hash_str = hash_data.get('hash')
        elif isinstance(hash_data, str): stored_hash_str = hash_data
        if stored_hash_str is None: continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            if (target_hash - stored_hash) <= threshold: return True # Found similar
        except ValueError: pass
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}': {e}", file=sys.stderr)
    return False

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
    else: return duplicates
    now = datetime.datetime.now(datetime.timezone.utc)
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None; original_user_id = None; timestamp_str = None
        if isinstance(hash_data, dict):
            stored_hash_str = hash_data.get('hash'); original_user_id = hash_data.get('user_id'); timestamp_str = hash_data.get('timestamp')
        elif isinstance(hash_data, str): stored_hash_str = hash_data
        if stored_hash_str is None: continue
        # Time Check
        if check_duration_days > 0 and timestamp_str:
            try:
                stored_time = dateutil.parser.isoparse(timestamp_str)
                if stored_time.tzinfo is None: stored_time = stored_time.replace(tzinfo=datetime.timezone.utc)
                if (now - stored_time).days > check_duration_days: continue
            except Exception: pass
        # Hash Comparison
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = new_image_hash - stored_hash
            if distance <= threshold:
                original_message_id = None
                try: original_message_id = int(identifier.split('-')[0])
                except: pass
                duplicates.append({'identifier': identifier, 'distance': distance, 'original_message_id': original_message_id, 'original_user_id': original_user_id})
        except ValueError: pass
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}': {e}", file=sys.stderr)
    duplicates.sort(key=lambda x: x['distance'])
    return duplicates

async def find_duplicates(new_image_hash, stored_hashes_dict, threshold, scope, channel_id, check_duration_days, loop):
    """Async wrapper for duplicate finding."""
    func = partial(find_duplicates_sync, new_image_hash, stored_hashes_dict, threshold, scope, str(channel_id), check_duration_days)
    duplicates = await loop.run_in_executor(None, func)
    return duplicates


# --- Discord Bot Implementation ---
intents = discord.Intents.default(); intents.messages = True; intents.message_content = True; intents.guilds = True; intents.reactions = True
# Define bot with help command attributes
bot = commands.Bot(
    command_prefix=DEFAULT_COMMAND_PREFIX,
    intents=intents,
    help_command=commands.DefaultHelpCommand(
        no_category = 'Commands' # Group all commands under 'Commands'
    )
)

# --- Event Handlers ---
@bot.event
async def on_ready():
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---'); print(f'--- Command Prefix: {bot.command_prefix} ---')
    await load_main_config()
    print(f'--- Ready for {len(bot.guilds)} guilds ---')
    for guild in bot.guilds: _ = get_guild_config(guild.id)
    print('------')

@bot.event
async def on_guild_join(guild):
     print(f"Joined new guild: {guild.name} (ID: {guild.id})"); _ = get_guild_config(guild.id); await save_main_config()

@bot.event
async def on_message(message):
    """Handles image processing and duplicate checks based on scope, mode, and time."""
    if message.guild is None or message.author == bot.user or message.author.bot: return

    # Check if user is allowlisted before processing commands or messages
    guild_id = message.guild.id
    guild_config = get_guild_config(guild_id)
    current_user_id = message.author.id
    allowed_users = guild_config.get('allowed_users', [])
    if current_user_id in allowed_users:
        # print(f"DEBUG: [G:{guild_id}] User {current_user_id} is allowlisted. Skipping checks.") # Optional debug
        # Still process commands even if allowlisted
        await bot.process_commands(message)
        return # Skip duplicate checking entirely

    # Process commands if user not allowlisted
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    if ctx.valid: return # Don't process if it was a command

    # --- Image Processing Logic (Only if not allowlisted and not a command) ---
    channel_id = message.channel.id
    channel_id_str = str(channel_id)

    # Extract settings
    allowed_channel_ids = guild_config.get('allowed_channel_ids')
    current_scope = guild_config.get('duplicate_scope', 'server')
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    current_hash_size = guild_config.get('hash_size', 8)
    current_similarity_threshold = guild_config.get('similarity_threshold', 5)
    react_to_duplicates = guild_config.get('react_to_duplicates', True)
    delete_duplicates = guild_config.get('delete_duplicates', False)
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')

    if allowed_channel_ids and channel_id not in allowed_channel_ids: return
    if not message.attachments: return

    loop = asyncio.get_running_loop()
    stored_hashes = await load_guild_hashes(guild_id, loop)
    db_updated = False

    for i, attachment in enumerate(message.attachments):
        if attachment.content_type and attachment.content_type.startswith('image/'):
            try:
                image_bytes = await attachment.read()
                new_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                if new_hash is None: continue

                duplicate_matches = await find_duplicates(
                    new_hash, stored_hashes, current_similarity_threshold,
                    current_scope, channel_id, current_duration, loop
                )

                is_violation = False; violating_match = None
                if duplicate_matches:
                    if current_mode == "strict": is_violation = True; violating_match = duplicate_matches[0]
                    elif current_mode == "owner_allowed":
                        for match in duplicate_matches:
                            if match.get('original_user_id') is None or match.get('original_user_id') != current_user_id:
                                is_violation = True; violating_match = match; break
                    # if is_violation: print(f"DEBUG: [G:{guild_id}] Duplicate Violation Found!") # Can be noisy
                    # else: print(f"DEBUG: [G:{guild_id}] Duplicate Found, but owner allowed.") # Can be noisy

                # --- Handle Violation ---
                if is_violation and violating_match:
                    identifier = violating_match['identifier']; distance = violating_match['distance']
                    original_message_id = violating_match.get('original_message_id'); original_user_id = violating_match.get('original_user_id')
                    reply_text = f"{duplicate_reaction_emoji} Hold on, {message.author.mention}! Image `{attachment.filename}` similar to recent submission (ID: `{identifier}`, Dist: {distance}"
                    if original_user_id: reply_text += f", Orig User: <@{original_user_id}>"
                    reply_text += ")."
                    if original_message_id and message.guild:
                         try: jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"; reply_text += f"\nOriginal: {jump_url}"
                         except: pass
                    await message.reply(reply_text, mention_author=True)
                    if react_to_duplicates: try: await message.add_reaction(duplicate_reaction_emoji); except: pass
                    if delete_duplicates:
                        if message.channel.permissions_for(message.guild.me).manage_messages: try: await message.delete(); except: pass
                        else: print(f"DEBUG: [G:{guild_id}] Lacking 'Manage Messages' permission.")

                # --- Add Unique Hash (if no violation) ---
                elif not is_violation:
                    # Check if this exact hash already exists before adding
                    hash_exists = find_hash_exists_sync(new_hash, stored_hashes, 0, current_scope, channel_id_str)
                    if not hash_exists:
                        # print(f"DEBUG: [G:{guild_id} Scope:{current_scope}] Adding unique hash for '{attachment.filename}'.") # Can be noisy
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
                    # else: print(f"DEBUG: [G:{guild_id} Scope:{current_scope}] Hash already exists (allowed repost). Not re-adding.")

            except Exception as e: print(f"DEBUG: [G:{guild_id}] Error processing attachment '{attachment.filename}': {e}", file=sys.stderr); traceback.print_exc()

    if db_updated: await save_guild_hashes(guild_id, stored_hashes, loop)


# --- Configuration Commands (Duration Aware) ---

# Decorators must be on separate lines
@bot.group(name="config", invoke_without_command=True, help="Manage bot configuration for this server (Admin only). Shows current config if no subcommand.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def configcmd(ctx):
    """Base command shows current server settings."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id)
    embed = discord.Embed(title=f"Bot Configuration for {ctx.guild.name}", color=discord.Color.blue())
    scope = guild_config.get('duplicate_scope', 'server'); mode = guild_config.get('duplicate_check_mode', 'strict')
    duration = guild_config.get('duplicate_check_duration_days', 0); duration_str = f"{duration} days" if duration > 0 else "Forever"
    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True); embed.add_field(name="Check Mode", value=f"`{mode}`", inline=True); embed.add_field(name="Check Duration", value=f"`{duration_str}`", inline=True)
    for key, value in guild_config.items():
        if key in ['duplicate_scope', 'duplicate_check_mode', 'duplicate_check_duration_days']: continue
        display_value = value
        if key == 'allowed_channel_ids': display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels"
        elif key == 'hash_db_file': display_value = f"`{value}`"
        elif key == 'allowed_users': display_value = ', '.join(f'<@{u_id}>' for u_id in value) if value else "None"
        elif isinstance(value, bool): display_value = "Enabled" if value else "Disabled"
        embed.add_field(name=key.replace('_', ' ').title(), value=display_value, inline=False)
    await ctx.send(embed=embed)

@configcmd.error
async def config_error(ctx, error):
    """Error handler for the config command group."""
    if isinstance(error, commands.NoPrivateMessage): await ctx.send("❌ Config commands only work in a server.")
    elif isinstance(error, commands.MissingPermissions): await ctx.send("❌ Admin permissions required.")
    elif isinstance(error, commands.CommandInvokeError): await ctx.send(f"An error occurred: {error.original}"); traceback.print_exc()
    else: await ctx.send(f"An error occurred: {error}")

# Decorators must be on separate lines
@configcmd.command(name='set', help="Sets a config value. Usage: !config set <setting> <value>")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_set(ctx, setting: str, *, value: str):
    """Sets a configuration value for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); setting = setting.lower().replace('-', '_')
    valid_settings = ['similarity_threshold', 'hash_size', 'react_to_duplicates', 'delete_duplicates', 'duplicate_reaction_emoji', 'duplicate_scope', 'duplicate_check_mode', 'duplicate_check_duration_days']
    if setting not in valid_settings: await ctx.send(f"❌ Unknown setting. Use: `{', '.join(valid_settings)}`"); return
    original_value = guild_config.get(setting); new_value = None; error_msg = None
    try:
        if setting == 'duplicate_check_duration_days': new_value = int(value); assert new_value >= 0
        elif setting == 'duplicate_scope': value_lower = value.lower(); assert value_lower in VALID_SCOPES; new_value = value_lower
        elif setting == 'duplicate_check_mode': value_lower = value.lower(); assert value_lower in VALID_CHECK_MODES; new_value = value_lower
        elif setting == 'similarity_threshold': new_value = int(value); assert new_value >= 0
        elif setting == 'hash_size': new_value = int(value); assert new_value >= 4
        elif setting in ['react_to_duplicates', 'delete_duplicates']:
            if value.lower() in ['true', 'on', 'yes', '1', 'enable', 'enabled']: new_value = True
            elif value.lower() in ['false', 'off', 'no', '0', 'disable', 'disabled']: new_value = False
            else: error_msg = "Value must be true/false (or on/off, etc.)."
        elif setting == 'duplicate_reaction_emoji': try: await ctx.message.add_reaction(value); await ctx.message.remove_reaction(value, ctx.me); new_value = value; except: error_msg = "Invalid emoji."
    except (ValueError, AssertionError): error_msg = f"Invalid value format/range for '{setting}'."
    except Exception as e: error_msg = f"Unexpected validation error: {e}"
    if error_msg: await ctx.send(f"❌ Error setting '{setting}': {error_msg}"); return
    is_boolean = setting in ['react_to_duplicates', 'delete_duplicates']
    if new_value is not None or (is_boolean and new_value is False) :
        guild_config[setting] = new_value
        if await save_guild_config(guild_id, guild_config):
            display_val = f"{new_value} days" if setting == 'duplicate_check_duration_days' and new_value > 0 else ("Forever" if setting == 'duplicate_check_duration_days' else new_value)
            await ctx.send(f"✅ Updated '{setting}' from `{original_value}` to `{display_val}`.")
            if setting == 'duplicate_scope': await ctx.send(f"⚠️ **Warning:** Changing scope might affect hash storage/lookup.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"❌ Could not determine valid value for '{setting}'.")

# Decorators must be on separate lines
@configcmd.group(name='channel', invoke_without_command=True, help="Manage the allowed channel list for this server.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel(ctx):
    """Shows the list of channels currently being monitored."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id); channel_list = guild_config.get('allowed_channel_ids')
    if channel_list: await ctx.send(embed=discord.Embed(title=f"Allowed Channels for {ctx.guild.name}", description='\n'.join(f"- <#{ch_id}> (`{ch_id}`)" for ch_id in channel_list), color=discord.Color.blue()))
    else: await ctx.send("ℹ️ Monitoring all channels.")

# Decorators must be on separate lines
@config_channel.command(name='add', help="Adds a channel to the allowed list.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_add(ctx, channel: discord.TextChannel):
    """Adds a channel to the allowed list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') is None: guild_config['allowed_channel_ids'] = []
    if channel_id not in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].append(channel_id)
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Added <#{channel_id}>.")
        else: await ctx.send(f"⚠️ Failed save.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> already allowed.")

# Decorators must be on separate lines
@config_channel.command(name='remove', help="Removes a channel from the allowed list.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_remove(ctx, channel: discord.TextChannel):
    """Removes a channel from the allowed list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') and channel_id in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].remove(channel_id);
        if not guild_config['allowed_channel_ids']: guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Removed <#{channel_id}>.")
        else: await ctx.send(f"⚠️ Failed save.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> not in list.")

# Decorators must be on separate lines
@config_channel.command(name='clear', help="Clears the allowed channel list (monitors all).")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_clear(ctx):
    """Clears the allowed channel list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy()
    if guild_config.get('allowed_channel_ids') is not None:
        guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await ctx.send("✅ Cleared allowed channels.")
        else: await ctx.send(f"⚠️ Failed save.")
    else: await ctx.send("ℹ️ Already monitoring all.")

# --- Allowlist Commands ---

# Decorators must be on separate lines
@bot.group(name="allowlist", invoke_without_command=True, help="Manage users exempt from duplicate checks (Admin only).")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def allowlistcmd(ctx):
    """Shows the current user allowlist."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id)
    user_list = guild_config.get('allowed_users', [])

    if user_list:
        embed = discord.Embed(title=f"Allowlisted Users for {ctx.guild.name}", color=discord.Color.green())
        # Try to fetch user objects for mentions, fallback to ID if user not found/cached
        mentions = []
        for user_id in user_list:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id) # Fetch if not cached
            if user: mentions.append(f"- {user.mention} (`{user_id}`)")
            else: mentions.append(f"- *Unknown User* (`{user_id}`)")
        embed.description = '\n'.join(mentions)
        await ctx.send(embed=embed)
    else:
        await ctx.send("ℹ️ No users are currently allowlisted.")

@allowlistcmd.error
async def allowlist_error(ctx, error):
    """Error handler for the allowlist command group."""
    if isinstance(error, commands.NoPrivateMessage): await ctx.send("❌ Allowlist commands only work in a server.")
    elif isinstance(error, commands.MissingPermissions): await ctx.send("❌ Admin permissions required.")
    elif isinstance(error, commands.CommandInvokeError): await ctx.send(f"An error occurred: {error.original}"); traceback.print_exc()
    else: await ctx.send(f"An error occurred: {error}")

# Decorators must be on separate lines
@allowlistcmd.command(name='add', help="Adds a user to the allowlist (exempt from checks).")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def allowlist_add(ctx, user: discord.User):
    """Adds a user to the allowlist."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id).copy()
    user_id = user.id

    # Ensure the list exists
    if 'allowed_users' not in guild_config or guild_config['allowed_users'] is None:
        guild_config['allowed_users'] = []

    if user_id not in guild_config['allowed_users']:
        guild_config['allowed_users'].append(user_id)
        if await save_guild_config(guild_id, guild_config):
            await ctx.send(f"✅ Added {user.mention} (`{user_id}`) to the allowlist.")
        else:
            await ctx.send(f"⚠️ Failed to save configuration update.")
    else:
        await ctx.send(f"ℹ️ {user.mention} is already on the allowlist.")

# Decorators must be on separate lines
@allowlistcmd.command(name='remove', help="Removes a user from the allowlist.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def allowlist_remove(ctx, user: discord.User):
    """Removes a user from the allowlist."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id).copy()
    user_id = user.id

    if 'allowed_users' in guild_config and guild_config['allowed_users'] and user_id in guild_config['allowed_users']:
        guild_config['allowed_users'].remove(user_id)
        if await save_guild_config(guild_id, guild_config):
            await ctx.send(f"✅ Removed {user.mention} (`{user_id}`) from the allowlist.")
        else:
            await ctx.send(f"⚠️ Failed to save configuration update.")
    else:
        await ctx.send(f"ℹ️ {user.mention} was not found on the allowlist.")


# --- Hash Management Commands ---

# Helper function to parse message link/ID
def parse_message_id(message_ref: str) -> int | None:
    """Parses a message link or ID string into an integer ID."""
    # Regex to find message ID in links like .../channels/guild/channel/message
    match = re.search(r'/(\d+)$', message_ref)
    if match:
        return int(match.group(1))
    # Check if it's just a raw ID
    elif message_ref.isdigit():
        return int(message_ref)
    return None

# Decorators must be on separate lines
@bot.command(name='removehash', help="Removes the stored hash associated with a specific message ID or link.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def remove_hash(ctx, message_reference: str):
    """Removes a hash entry based on the original message ID."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server')
    loop = asyncio.get_running_loop()

    target_message_id = parse_message_id(message_reference)
    if target_message_id is None:
        await ctx.send("❌ Invalid message ID or link format.")
        return

    target_message_id_str = str(target_message_id)
    stored_hashes = await load_guild_hashes(guild_id, loop)
    hash_removed = False
    key_to_remove = None

    if not isinstance(stored_hashes, dict):
        await ctx.send("ℹ️ Hash database is empty or invalid.")
        return

    # Find the key associated with the message ID
    if current_scope == "server":
        for identifier in stored_hashes.keys():
            if identifier.startswith(target_message_id_str + "-"):
                key_to_remove = identifier
                break
        if key_to_remove:
            del stored_hashes[key_to_remove]
            hash_removed = True

    elif current_scope == "channel":
        for channel_id_str, channel_hashes in stored_hashes.items():
            if isinstance(channel_hashes, dict):
                 for identifier in channel_hashes.keys():
                      if identifier.startswith(target_message_id_str + "-"):
                           key_to_remove = identifier
                           del channel_hashes[key_to_remove] # Remove from nested dict
                           hash_removed = True
                           break # Assume only one entry per message ID
            if hash_removed: break # Stop searching channels

    # Save if removal occurred
    if hash_removed:
        if await save_guild_hashes(guild_id, stored_hashes, loop):
            await ctx.send(f"✅ Removed hash entry associated with message ID `{target_message_id}`.")
        else:
            await ctx.send("⚠️ Error saving updated hash database after removal.")
    else:
        await ctx.send(f"ℹ️ No hash entry found for message ID `{target_message_id}`.")

# Decorators must be on separate lines
@bot.command(name='clearhashes', help="Clears hashes for the server or a specific channel. Requires confirmation.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def clear_hashes(ctx, channel: typing.Optional[discord.TextChannel] = None, *, confirmation: str = ""):
    """Clears hashes. Use --confirm to proceed."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server')
    loop = asyncio.get_running_loop()

    if confirmation.lower() != '--confirm':
        cmd = f"`!clearhashes {f'#{channel.name} ' if channel else ''}--confirm`"
        await ctx.send(f"🛑 **Warning:** This will permanently delete stored hashes. "
                       f"To proceed, please run the command again with `--confirm` at the end.\n"
                       f"Example: {cmd}")
        return

    target_desc = f"channel <#{channel.id}>" if channel else "the entire server"
    status_msg = await ctx.send(f"⏳ Clearing hashes for {target_desc}...")

    stored_hashes = await load_guild_hashes(guild_id, loop)
    cleared = False

    if not isinstance(stored_hashes, dict):
         await status_msg.edit(content=f"ℹ️ Hash database is already empty or invalid for {target_desc}.")
         return

    if channel:
        # Clear specific channel
        target_channel_id_str = str(channel.id)
        if current_scope == "channel":
            if target_channel_id_str in stored_hashes:
                del stored_hashes[target_channel_id_str]
                cleared = True
            else:
                 await status_msg.edit(content=f"ℹ️ No hashes found specifically for channel <#{channel.id}> (Scope is 'channel').")
                 return # No need to save if nothing changed
        elif current_scope == "server":
             await status_msg.edit(content=f"ℹ️ Cannot clear specific channel when scope is 'server'. Use `!clearhashes --confirm` to clear all server hashes.")
             return # Don't proceed
    else:
        # Clear all hashes for the guild
        stored_hashes.clear()
        cleared = True

    if cleared:
        if await save_guild_hashes(guild_id, stored_hashes, loop):
            await status_msg.edit(content=f"✅ Successfully cleared hashes for {target_desc}.")
        else:
            await status_msg.edit(content=f"⚠️ Error saving cleared hash database for {target_desc}.")
    # else: # Message already sent if channel not found or scope mismatch
    #     pass


# --- Scan Command (Unchanged from previous version) ---
@bot.command(name='scan', help="Scans channel history to add unique image hashes. Usage: !scan <#channel> [limit]")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def scan_history(ctx, channel: discord.TextChannel, limit: typing.Optional[int] = DEFAULT_SCAN_LIMIT):
    """Scans past messages in a channel to populate the hash database."""
    guild_id = ctx.guild.id; scan_channel_id = channel.id; scan_channel_id_str = str(scan_channel_id)
    guild_config = get_guild_config(guild_id); current_scope = guild_config.get('duplicate_scope', 'server')
    current_hash_size = guild_config.get('hash_size', 8); existence_threshold = 0
    if not channel.permissions_for(ctx.guild.me).read_message_history: await ctx.send(f"❌ Bot lacks 'Read Message History' permission in <#{scan_channel_id}>."); return
    status_message = await ctx.send(f"⏳ Starting scan of up to **{limit}** messages in <#{scan_channel_id}>...")
    processed_messages = 0; added_hashes = 0; skipped_attachments = 0; errors = 0
    loop = asyncio.get_running_loop()
    stored_hashes = await load_guild_hashes(guild_id, loop); db_updated = False
    try:
        async for message in channel.history(limit=limit):
            processed_messages += 1
            if processed_messages % SCAN_UPDATE_INTERVAL == 0: try: await status_message.edit(content=f"⏳ Scanning... Processed {processed_messages}/{limit}. Added {added_hashes} new hashes."); except: pass
            if message.author.bot or not message.attachments: continue
            message_user_id = message.author.id; message_timestamp_iso = message.created_at.replace(tzinfo=datetime.timezone.utc).isoformat()
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    try:
                        image_bytes = await attachment.read(); img_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                        if img_hash is None: skipped_attachments += 1; continue
                        hash_exists = find_hash_exists_sync(img_hash, stored_hashes, existence_threshold, current_scope, scan_channel_id_str)
                        if not hash_exists:
                            added_hashes += 1; db_updated = True; unique_identifier = f"{message.id}-{attachment.filename}"
                            hash_data_to_store = {"hash": str(img_hash), "user_id": message_user_id, "timestamp": message_timestamp_iso}
                            if current_scope == "server":
                                if not isinstance(stored_hashes, dict): stored_hashes = {}
                                stored_hashes[unique_identifier] = hash_data_to_store
                            elif current_scope == "channel":
                                if not isinstance(stored_hashes, dict): stored_hashes = {}
                                channel_hashes = stored_hashes.setdefault(scan_channel_id_str, {})
                                if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[scan_channel_id_str] = channel_hashes
                                channel_hashes[unique_identifier] = hash_data_to_store
                    except discord.HTTPException as e: print(f"Warning: [Scan G:{guild_id}] Failed download {attachment.id}: {e}"); errors += 1; skipped_attachments += 1
                    except Exception as e: print(f"Error: [Scan G:{guild_id}] Error processing attach {attachment.id}: {e}"); errors += 1; skipped_attachments += 1; traceback.print_exc()
    except discord.Forbidden: await status_message.edit(content=f"❌ Scan failed. Bot lacks permissions in <#{scan_channel_id}>."); return
    except Exception as e: await status_message.edit(content=f"❌ Error during scan: {e}"); traceback.print_exc(); return
    finally:
        if db_updated:
            print(f"DEBUG: [Scan G:{guild_id}] Saving updated hash database after scan...")
            if not await save_guild_hashes(guild_id, stored_hashes, loop): await ctx.send("⚠️ Error saving hashes after scan.")
    await status_message.edit(content=f"✅ Scan Complete! Processed {processed_messages}. Added **{added_hashes}** new hashes. Skipped {skipped_attachments}. Errors: {errors}.")


# --- Main Execution ---
if __name__ == "__main__":
    try: import dateutil.parser
    except ImportError: print("Optional dependency 'python-dateutil' not found. Timestamps might not parse correctly. Consider: pip install python-dateutil")
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found.", file=sys.stderr); sys.exit(1)
    try: print("Starting bot..."); bot.run(BOT_TOKEN)
    except Exception as e: print(f"An error occurred: {e}", file=sys.stderr); traceback.print_exc()
    finally: print("DEBUG: Bot run loop finished or encountered an error.")


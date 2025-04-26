#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate images with per-server configuration.
Supports 'server' or 'channel' duplicate scope.
Supports 'strict' or 'owner_allowed' duplicate check modes.
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

# --- Constants ---
CONFIG_FILE_PATH = 'server_configs.json'
DEFAULT_COMMAND_PREFIX = "!"
HASH_FILENAME_PREFIX = "hashes_"
VALID_SCOPES = ["server", "channel"]
VALID_CHECK_MODES = ["strict", "owner_allowed"]

# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# --- Global Configuration Cache & Locks ---
server_configs = {}
config_lock = asyncio.Lock()
hash_file_locks = {}

# --- Configuration Loading/Saving ---

def get_default_guild_config(guild_id):
    """Returns default settings, including new check_mode."""
    return {
        "hash_db_file": f"{HASH_FILENAME_PREFIX}{guild_id}.json",
        "hash_size": 8,
        "similarity_threshold": 5,
        "allowed_channel_ids": None,
        "react_to_duplicates": True,
        "delete_duplicates": False,
        "duplicate_reaction_emoji": "⚠️",
        "duplicate_scope": "server",
        "duplicate_check_mode": "strict" # Default check mode
    }

def validate_config_data(config_data):
     """Validates config, including new check_mode."""
     validated = get_default_guild_config(0).copy()
     validated.update(config_data)
     try:
        validated['hash_size'] = int(validated['hash_size'])
        validated['similarity_threshold'] = int(validated['similarity_threshold'])
        validated['react_to_duplicates'] = bool(validated['react_to_duplicates'])
        validated['delete_duplicates'] = bool(validated['delete_duplicates'])
        # Validate scope
        if validated.get('duplicate_scope') not in VALID_SCOPES:
             validated['duplicate_scope'] = "server"
        # Validate check mode
        if validated.get('duplicate_check_mode') not in VALID_CHECK_MODES:
             print(f"Warning: Invalid 'duplicate_check_mode'. Resetting to 'strict'.", file=sys.stderr)
             validated['duplicate_check_mode'] = "strict"
        # Validate allowed_channel_ids
        if validated['allowed_channel_ids'] is not None:
            if isinstance(validated['allowed_channel_ids'], list):
                 validated['allowed_channel_ids'] = [int(ch_id) for ch_id in validated['allowed_channel_ids'] if str(ch_id).isdigit()]
            else:
                 validated['allowed_channel_ids'] = None
     except Exception as e:
          print(f"Warning: Error validating config types: {e}.", file=sys.stderr)
     return validated

async def load_main_config():
    """Loads the main server_configs.json file."""
    global server_configs
    async with config_lock:
        print(f"DEBUG: Loading main config file: {CONFIG_FILE_PATH}")
        try:
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
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
        print(f"DEBUG: Saving main config file: {CONFIG_FILE_PATH}")
        config_to_save = {str(gid): data for gid, data in server_configs.items()}
        try:
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f: json.dump(config_to_save, f, indent=4)
            print(f"DEBUG: Successfully saved main config for {len(config_to_save)} guilds.")
            return True
        except Exception as e: print(f"DEBUG: Error saving main config: {e}"); return False

def get_guild_config(guild_id):
    """Gets guild config, ensures defaults exist (incl. check_mode)."""
    global server_configs
    defaults_needed = False
    if guild_id not in server_configs:
        print(f"DEBUG: No config found for guild {guild_id}. Creating defaults.")
        server_configs[guild_id] = get_default_guild_config(guild_id)
        defaults_needed = True
    else:
        # Ensure existing configs have the new fields
        guild_conf = server_configs[guild_id]
        default_conf = get_default_guild_config(guild_id)
        for key, default_value in default_conf.items():
            if key not in guild_conf:
                 print(f"DEBUG: Adding missing default key '{key}' for guild {guild_id}.")
                 guild_conf[key] = default_value
                 defaults_needed = True
        # Re-validate after potentially adding keys
        server_configs[guild_id] = validate_config_data(guild_conf)

    if defaults_needed:
        asyncio.create_task(save_main_config()) # Schedule save if defaults were added/updated

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
    except: return None # Simplified error handling
async def calculate_hash(image_bytes, hash_size, loop):
    func = partial(calculate_hash_sync, image_bytes, hash_size); return await loop.run_in_executor(None, func)

def load_hashes_sync(db_file):
    if not os.path.exists(db_file): return {}
    try:
        with open(db_file, 'r', encoding='utf-8') as f: data = json.load(f)
        if not isinstance(data, dict): return {}
        # Basic check for new format - look for nested 'hash' key in a value
        # This isn't foolproof but helps detect format mismatch
        is_new_format = False
        for v in data.values():
            if isinstance(v, dict) and 'hash' in v: is_new_format = True; break
            if isinstance(v, dict): # Check nested dict for channel scope
                 for subv in v.values():
                      if isinstance(subv, dict) and 'hash' in subv: is_new_format = True; break
            if is_new_format: break
        if not is_new_format and data: # If file has data but doesn't look like new format
             print(f"Warning: Hash file '{db_file}' seems to be in old format (no user_id). Owner checks may fail.", file=sys.stderr)

        print(f"DEBUG: Loaded hashes from '{db_file}'.")
        return data
    except Exception as e: print(f"DEBUG: Error loading hash db '{db_file}': {e}"); return {}

def save_hashes_sync(hashes_dict, db_file):
    try:
        with open(db_file, 'w', encoding='utf-8') as f: json.dump(hashes_dict, f, indent=4)
        # print(f"DEBUG: Saved hashes to '{db_file}'.") # Can be noisy
        return True
    except Exception as e: print(f"DEBUG: Error saving hash db '{db_file}': {e}"); return False

async def load_guild_hashes(guild_id, loop):
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(load_hashes_sync, db_file); return await loop.run_in_executor(None, func)

async def save_guild_hashes(guild_id, hashes_dict, loop):
    guild_config = get_guild_config(guild_id); db_file = guild_config['hash_db_file']; lock = get_hash_file_lock(guild_id)
    async with lock: func = partial(save_hashes_sync, hashes_dict, db_file); return await loop.run_in_executor(None, func)

# --- Duplicate Finding (Scope Aware, Returns UserID) ---
def find_duplicates_sync(new_image_hash, stored_hashes_dict, threshold, scope, channel_id_str):
    """
    Finds duplicates based on scope. Returns original user ID if found.
    Handles both old ({id: hash}) and new ({id: {hash:..., user_id:...}}) formats.
    """
    duplicates = []
    if new_image_hash is None: return duplicates

    hashes_to_check = {}
    # Select the correct dictionary portion based on scope
    if scope == "server":
        if isinstance(stored_hashes_dict, dict): hashes_to_check = stored_hashes_dict
    elif scope == "channel":
        if isinstance(stored_hashes_dict, dict):
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else: return duplicates # Unknown scope

    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        original_user_id = None # Important: Default to None if not found

        # Determine hash string and user ID based on stored format
        if isinstance(hash_data, dict) and 'hash' in hash_data: # New format
            stored_hash_str = hash_data.get('hash')
            original_user_id = hash_data.get('user_id') # Will be None if key missing
        elif isinstance(hash_data, str): # Old format (assume it's just the hash string)
            stored_hash_str = hash_data
            # original_user_id remains None

        if stored_hash_str is None: continue # Skip if hash string couldn't be extracted

        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = new_image_hash - stored_hash
            if distance <= threshold:
                original_message_id = None
                try: original_message_id = int(identifier.split('-')[0])
                except: pass
                duplicates.append({
                    'identifier': identifier,
                    'distance': distance,
                    'original_message_id': original_message_id,
                    'original_user_id': original_user_id # Add user id to result
                })
        except ValueError: pass # Ignore invalid hash strings silently now
        except Exception as e: print(f"DEBUG: Error comparing hash for identifier '{identifier}': {e}", file=sys.stderr)

    duplicates.sort(key=lambda x: x['distance'])
    return duplicates

async def find_duplicates(new_image_hash, stored_hashes_dict, threshold, scope, channel_id, loop):
    """Async wrapper for duplicate finding."""
    func = partial(find_duplicates_sync, new_image_hash, stored_hashes_dict, threshold, scope, str(channel_id))
    duplicates = await loop.run_in_executor(None, func)
    return duplicates


# --- Discord Bot Implementation ---
intents = discord.Intents.default(); intents.messages = True; intents.message_content = True; intents.guilds = True; intents.reactions = True
bot = commands.Bot(command_prefix=DEFAULT_COMMAND_PREFIX, intents=intents)

# --- Event Handlers ---
@bot.event
async def on_ready():
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---'); print(f'--- Command Prefix: {bot.command_prefix} ---')
    await load_main_config()
    print(f'--- Ready for {len(bot.guilds)} guilds ---')
    for guild in bot.guilds: _ = get_guild_config(guild.id) # Ensure configs exist
    print('------')

@bot.event
async def on_guild_join(guild):
     print(f"Joined new guild: {guild.name} (ID: {guild.id})"); _ = get_guild_config(guild.id); await save_main_config()

@bot.event
async def on_message(message):
    """Handles image processing and duplicate checks based on scope and mode."""
    if message.guild is None or message.author == bot.user or message.author.bot: return
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    if ctx.valid: return

    guild_id = message.guild.id
    channel_id = message.channel.id
    channel_id_str = str(channel_id)
    current_user_id = message.author.id
    guild_config = get_guild_config(guild_id)

    allowed_channel_ids = guild_config.get('allowed_channel_ids')
    current_scope = guild_config.get('duplicate_scope', 'server')
    current_mode = guild_config.get('duplicate_check_mode', 'strict') # Get check mode
    current_hash_size = guild_config.get('hash_size')
    current_similarity_threshold = guild_config.get('similarity_threshold')
    react_to_duplicates = guild_config.get('react_to_duplicates')
    delete_duplicates = guild_config.get('delete_duplicates')
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji')

    if allowed_channel_ids and channel_id not in allowed_channel_ids: return
    if not message.attachments: return

    # print(f"DEBUG: [G:{guild_id} C:{channel_id}] Msg with attachments received.") # Less verbose

    loop = asyncio.get_running_loop()
    stored_hashes = await load_guild_hashes(guild_id, loop)
    db_updated = False

    for i, attachment in enumerate(message.attachments):
        if attachment.content_type and attachment.content_type.startswith('image/'):
            try:
                image_bytes = await attachment.read()
                new_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                if new_hash is None: continue

                duplicate_matches = await find_duplicates(new_hash, stored_hashes, current_similarity_threshold, current_scope, channel_id, loop)

                is_violation = False
                violating_match = None

                if duplicate_matches:
                    if current_mode == "strict":
                        is_violation = True
                        violating_match = duplicate_matches[0] # Closest match
                        print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:Strict] Duplicate Found!")
                    elif current_mode == "owner_allowed":
                        # Check if any match is from a *different* user
                        for match in duplicate_matches:
                            # If original_user_id is None (e.g., old format), treat as violation to be safe
                            if match.get('original_user_id') is None or match.get('original_user_id') != current_user_id:
                                is_violation = True
                                violating_match = match
                                print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:OwnerAllowed] Duplicate Found (Orig User: {match.get('original_user_id')}, Curr User: {current_user_id})")
                                break # Found a violation, no need to check further
                        if not is_violation:
                             print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:OwnerAllowed] Duplicate Found, but current user is owner. Allowing.")

                # --- Handle Violation (if any) ---
                if is_violation and violating_match:
                    identifier = violating_match['identifier']
                    distance = violating_match['distance']
                    original_message_id = violating_match.get('original_message_id')
                    original_user_id = violating_match.get('original_user_id')

                    reply_text = (
                        f"{duplicate_reaction_emoji} Hold on, {message.author.mention}! Image `{attachment.filename}` is similar "
                        f"to a prior submission (ID: `{identifier}`, Dist: {distance}"
                    )
                    if original_user_id:
                         reply_text += f", Orig User: <@{original_user_id}>"
                    reply_text += ")."

                    if original_message_id and message.guild:
                         jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                         reply_text += f"\nOriginal might be here: {jump_url}"
                    await message.reply(reply_text, mention_author=True)

                    if react_to_duplicates:
                        try: await message.add_reaction(duplicate_reaction_emoji)
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed reaction: {e}")
                    if delete_duplicates:
                        try: await message.delete()
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed delete: {e}")

                # --- Add Unique Hash (if no violation occurred) ---
                elif not duplicate_matches: # Only add if no matches were found at all
                    print(f"DEBUG: [G:{guild_id} Scope:{current_scope}] Image '{attachment.filename}' unique. Adding.")
                    unique_identifier = f"{message.id}-{attachment.filename}"
                    # Store in new format: { "hash": "...", "user_id": ... }
                    hash_data_to_store = {"hash": str(new_hash), "user_id": current_user_id}

                    if current_scope == "server":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        stored_hashes[unique_identifier] = hash_data_to_store
                    elif current_scope == "channel":
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        channel_hashes = stored_hashes.setdefault(channel_id_str, {})
                        if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[channel_id_str] = channel_hashes
                        channel_hashes[unique_identifier] = hash_data_to_store

                    db_updated = True

            except Exception as e:
                print(f"DEBUG: [G:{guild_id}] Error processing attachment '{attachment.filename}': {e}", file=sys.stderr)
                traceback.print_exc()

    if db_updated:
        print(f"DEBUG: [G:{guild_id}] Saving updated hash database...")
        await save_guild_hashes(guild_id, stored_hashes, loop)


# --- Configuration Commands (Scope/Mode Aware) ---

@bot.group(name="config", invoke_without_command=True)
@commands.guild_only() @commands.has_permissions(administrator=True)
async def configcmd(ctx):
    """Base command shows current server settings."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id)
    embed = discord.Embed(title=f"Bot Configuration for {ctx.guild.name}", color=discord.Color.blue())

    # Display scope and mode first
    scope = guild_config.get('duplicate_scope', 'server')
    mode = guild_config.get('duplicate_check_mode', 'strict')
    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True)
    embed.add_field(name="Duplicate Check Mode", value=f"`{mode}`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True) # Spacer

    for key, value in guild_config.items():
        if key in ['duplicate_scope', 'duplicate_check_mode']: continue # Already displayed
        display_value = value
        if key == 'allowed_channel_ids': display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels"
        elif key == 'hash_db_file': display_value = f"`{value}`"
        elif isinstance(value, bool): display_value = "Enabled" if value else "Disabled"
        embed.add_field(name=key.replace('_', ' ').title(), value=display_value, inline=False)
    await ctx.send(embed=embed)

@configcmd.error
async def config_error(ctx, error):
    if isinstance(error, commands.NoPrivateMessage): await ctx.send("❌ Config commands only work in a server.")
    elif isinstance(error, commands.MissingPermissions): await ctx.send("❌ Admin permissions required.")
    elif isinstance(error, commands.CommandInvokeError): await ctx.send(f"An error occurred: {error.original}"); traceback.print_exc()
    else: await ctx.send(f"An error occurred: {error}")

@configcmd.command(name='set')
@commands.guild_only() @commands.has_permissions(administrator=True)
async def config_set(ctx, setting: str, *, value: str):
    """Sets a config value. Usage: !config set <setting> <value>"""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy()
    setting = setting.lower().replace('-', '_')

    valid_settings = [
        'similarity_threshold', 'hash_size', 'react_to_duplicates',
        'delete_duplicates', 'duplicate_reaction_emoji',
        'duplicate_scope', 'duplicate_check_mode' # Added new settings
    ]
    if setting not in valid_settings:
        await ctx.send(f"❌ Unknown setting. Settable keys: `{', '.join(valid_settings)}`"); return

    original_value = guild_config.get(setting); new_value = None; error_msg = None

    try:
        if setting == 'duplicate_scope':
            value_lower = value.lower()
            if value_lower in VALID_SCOPES: new_value = value_lower
            else: error_msg = f"Invalid scope. Use: `{', '.join(VALID_SCOPES)}`"
        elif setting == 'duplicate_check_mode':
             value_lower = value.lower()
             if value_lower in VALID_CHECK_MODES: new_value = value_lower
             else: error_msg = f"Invalid mode. Use: `{', '.join(VALID_CHECK_MODES)}`"
        elif setting == 'similarity_threshold': new_value = int(value); assert new_value >= 0
        elif setting == 'hash_size': new_value = int(value); assert new_value >= 4
        elif setting in ['react_to_duplicates', 'delete_duplicates']:
            if value.lower() in ['true', 'on', 'yes', '1', 'enable', 'enabled']: new_value = True
            elif value.lower() in ['false', 'off', 'no', '0', 'disable', 'disabled']: new_value = False
            else: error_msg = "Value must be true or false (or on/off, etc.)."
        elif setting == 'duplicate_reaction_emoji':
            try: await ctx.message.add_reaction(value); await ctx.message.remove_reaction(value, ctx.me); new_value = value
            except: error_msg = "Invalid emoji."
    except (ValueError, AssertionError): error_msg = f"Invalid value format/range for '{setting}'."
    except Exception as e: error_msg = f"Unexpected validation error: {e}"

    if error_msg: await ctx.send(f"❌ Error setting '{setting}': {error_msg}"); return

    # Allow setting boolean to False explicitly
    is_boolean_setting = setting in ['react_to_duplicates', 'delete_duplicates']
    if new_value is not None or (is_boolean_setting and new_value is False) :
        guild_config[setting] = new_value
        if await save_guild_config(guild_id, guild_config):
            await ctx.send(f"✅ Updated '{setting}' for this server from `{original_value}` to `{new_value}`.")
            if setting == 'duplicate_scope':
                 await ctx.send(f"⚠️ **Warning:** Changing scope might affect how existing hashes are read. Consider clearing the hash file (`{guild_config['hash_db_file']}`) if switching scope with existing data.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"❌ Could not determine valid value for '{setting}' from input '{value}'.")

# Channel commands remain unchanged
@configcmd.group(name='channel', invoke_without_command=True)
@commands.guild_only() @commands.has_permissions(administrator=True)
async def config_channel(ctx):
    # ... (same as before) ...
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id)
    channel_list = guild_config.get('allowed_channel_ids')
    if channel_list:
        embed = discord.Embed(title=f"Allowed Channels for {ctx.guild.name}", description='\n'.join(f"- <#{ch_id}> (`{ch_id}`)" for ch_id in channel_list), color=discord.Color.blue())
        await ctx.send(embed=embed)
    else: await ctx.send("ℹ️ Monitoring all channels in this server.")

@config_channel.command(name='add')
@commands.guild_only() @commands.has_permissions(administrator=True)
async def config_channel_add(ctx, channel: discord.TextChannel):
    # ... (same as before) ...
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') is None: guild_config['allowed_channel_ids'] = []
    if channel_id not in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].append(channel_id)
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Added <#{channel_id}> to allowed list.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> already allowed.")

@config_channel.command(name='remove')
@commands.guild_only() @commands.has_permissions(administrator=True)
async def config_channel_remove(ctx, channel: discord.TextChannel):
    # ... (same as before) ...
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') and channel_id in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].remove(channel_id)
        if not guild_config['allowed_channel_ids']: guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Removed <#{channel_id}> from allowed list.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> not in allowed list.")

@config_channel.command(name='clear')
@commands.guild_only() @commands.has_permissions(administrator=True)
async def config_channel_clear(ctx):
    # ... (same as before) ...
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy()
    if guild_config.get('allowed_channel_ids') is not None:
        guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await ctx.send("✅ Cleared allowed channels. Monitoring all.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send("ℹ️ Already monitoring all channels.")

# --- Main Execution ---
if __name__ == "__main__":
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found.", file=sys.stderr); sys.exit(1)
    try: print("Starting bot..."); bot.run(BOT_TOKEN)
    except Exception as e: print(f"An error occurred: {e}", file=sys.stderr); traceback.print_exc()
    finally: print("DEBUG: Bot run loop finished or encountered an error.")

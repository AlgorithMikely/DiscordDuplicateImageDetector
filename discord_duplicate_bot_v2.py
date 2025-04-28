#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate images using Slash Commands.
Supports per-server configuration, scope, check mode, time limits,
history scanning (/scan - finds oldest, optionally flags/replies/deletes non-oldest),
hash management (/hash_remove, /hash_clear), user allowlisting (/allowlist_add etc.),
retroactive flag clearing (/clearflags), and configurable reply messages.
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
SCAN_UPDATE_INTERVAL = 100
SCAN_ACTION_DELAY = 0.35 # Delay for scan actions (flag/reply/delete)
CLEAR_REACTION_DELAY = 0.2 # Delay for clearing reactions

# Default Reply Template Placeholders:
# {mention}: Mention of the user who posted the duplicate.
# {filename}: Original filename of the duplicate image.
# {identifier}: The internal identifier for the matched original image (message_id-filename).
# {distance}: The hash distance (similarity score) between the images.
# {original_user_mention}: Mention of the user who posted the original image (if known).
# {jump_url}: Link to the original message (if possible).
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
    """Returns default settings, including allowlist and reply template."""
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
        "allowed_users": [], # List of user IDs exempt from checks
        "duplicate_reply_template": DEFAULT_REPLY_TEMPLATE # New setting
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

        # Ensure reply template is a string
        if not isinstance(validated.get('duplicate_reply_template'), str):
            validated['duplicate_reply_template'] = DEFAULT_REPLY_TEMPLATE

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


# --- Hashing and File I/O Functions (Remain the same) ---

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
        is_new_format_likely = False; has_timestamp = False # Format check
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
    else: return None, None
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        if isinstance(hash_data, dict) and 'hash' in hash_data: stored_hash_str = hash_data.get('hash')
        elif isinstance(hash_data, str): stored_hash_str = hash_data
        if stored_hash_str is None: continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            if (target_hash - stored_hash) <= threshold: return identifier, hash_data
        except ValueError: pass
        except Exception as e: print(f"DEBUG: Error comparing hash for '{identifier}': {e}", file=sys.stderr)
    return None, None

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
intents = discord.Intents.default(); intents.message_content = True; intents.guilds = True; intents.members = True; intents.reactions = True
bot = discord.Bot(intents=intents)

# --- Event Handlers ---
@bot.event
async def on_ready():
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---')
    await load_main_config()
    print(f'--- Ready for {len(bot.guilds)} guilds ---')
    for guild in bot.guilds: _ = get_guild_config(guild.id)
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
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE) # Get template

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

                # --- Handle Violation ---
                if is_violation and violating_match:
                    identifier = violating_match['identifier']; distance = violating_match['distance']
                    original_message_id = violating_match.get('original_message_id'); original_user_id = violating_match.get('original_user_id')

                    # --- Format Reply using Template ---
                    template_data = {
                        "mention": message.author.mention,
                        "filename": attachment.filename,
                        "identifier": identifier,
                        "distance": distance,
                        "original_user_mention": f"<@{original_user_id}>" if original_user_id else "*Unknown*",
                        "emoji": duplicate_reaction_emoji,
                        "original_user_info": f", Orig User: <@{original_user_id}>" if original_user_id else "", # Optional part
                        "jump_link": "" # Placeholder for jump link
                    }
                    if original_message_id and message.guild:
                        try:
                            jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                            template_data["jump_link"] = f"\nOriginal: {jump_url}"
                        except: pass # Ignore errors creating jump url

                    # Use safe formatting (ignore missing keys)
                    reply_text = reply_template.format_map(defaultdict(str, template_data))

                    await message.reply(reply_text, mention_author=True)

                    if react_to_duplicates:
                        try: await message.add_reaction(duplicate_reaction_emoji)
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed reaction: {e}")
                    if delete_duplicates:
                        if message.channel.permissions_for(message.guild.me).manage_messages:
                            try: await message.delete()
                            except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed delete: {e}")
                        else: print(f"DEBUG: [G:{guild_id}] Lacking 'Manage Messages' permission.")

                # --- Add Unique Hash (if no violation) ---
                elif not is_violation:
                    # Check if this exact hash already exists before adding
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

            except Exception as e: print(f"DEBUG: [G:{guild_id}] Error processing attachment '{attachment.filename}': {e}", file=sys.stderr); traceback.print_exc()

    if db_updated: await save_guild_hashes(guild_id, stored_hashes, loop)


# --- Slash Command Definitions ---

# Helper for permission checks
async def check_admin_permissions(interaction: discord.Interaction) -> bool:
    """Checks if the invoking user has administrator permissions."""
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True); return False
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permissions.", ephemeral=True); return False
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
    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True); embed.add_field(name="Check Mode", value=f"`{mode}`", inline=True); embed.add_field(name="Check Duration", value=f"`{duration_str}`", inline=True)
    for key, value in guild_config.items():
        if key in ['duplicate_scope', 'duplicate_check_mode', 'duplicate_check_duration_days']: continue
        display_value = value
        if key == 'allowed_channel_ids': display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels"
        elif key == 'hash_db_file': display_value = f"`{value}`"
        elif key == 'allowed_users': display_value = ', '.join(f'<@{u_id}>' for u_id in value) if value else "None"
        elif key == 'duplicate_reply_template': display_value = f"```\n{value}\n```" # Show template in code block
        elif isinstance(value, bool): display_value = "Enabled" if value else "Disabled"
        embed.add_field(name=key.replace('_', ' ').title(), value=display_value, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def _update_config(interaction: discord.Interaction, setting: str, new_value: typing.Any):
    """Helper to update and save config, sending response."""
    guild_id = interaction.guild_id
    guild_config = get_guild_config(guild_id).copy()
    original_value = guild_config.get(setting)
    guild_config[setting] = new_value
    if await save_guild_config(guild_id, guild_config):
        display_new = new_value
        if setting == 'duplicate_check_duration_days': display_new = f"{new_value} days" if new_value > 0 else "Forever"
        elif setting == 'duplicate_reply_template': display_new = f"```\n{new_value}\n```" # Show template in code block
        await interaction.response.send_message(f"✅ Updated '{setting}' from `{original_value}` to `{display_new}`.", ephemeral=True)
        if setting == 'duplicate_scope': await interaction.followup.send(f"⚠️ **Warning:** Changing scope might affect hash lookup.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Failed to save config update.", ephemeral=True)

@bot.slash_command(name="config_set_threshold", description="Sets the similarity threshold (0-20, lower is stricter).")
async def config_set_threshold(interaction: discord.Interaction, value: discord.Option(int, "New threshold value (0=exact match).", min_value=0, max_value=20)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await _update_config(interaction, "similarity_threshold", value)

@bot.slash_command(name="config_set_hash_size", description="Sets the hash detail level (e.g., 8 or 16).")
async def config_set_hash_size(interaction: discord.Interaction, value: discord.Option(int, "New hash size (must be >= 4).", min_value=4, max_value=32)):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await _update_config(interaction, "hash_size", value)

@bot.slash_command(name="config_set_react", description="Enable/disable reacting to duplicate messages.")
async def config_set_react(interaction: discord.Interaction, value: discord.Option(bool, "True to enable, False to disable.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await _update_config(interaction, "react_to_duplicates", value)

@bot.slash_command(name="config_set_delete", description="Enable/disable deleting duplicate messages.")
async def config_set_delete(interaction: discord.Interaction, value: discord.Option(bool, "True to enable, False to disable.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    await _update_config(interaction, "delete_duplicates", value)

@bot.slash_command(name="config_set_emoji", description="Sets the emoji used for duplicate reactions.")
async def config_set_emoji(interaction: discord.Interaction, value: discord.Option(str, "The new emoji.")):
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    # Basic validation: try reacting to check if usable (might fail silently later if invalid custom)
    try: await interaction.response.defer(ephemeral=True); await interaction.followup.send("Testing emoji...", ephemeral=True); msg = await interaction.original_response(); await msg.add_reaction(value); await msg.remove_reaction(value, bot.user)
    except Exception: await interaction.edit_original_response(content="❌ Invalid or inaccessible emoji provided."); return
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

@bot.slash_command(name="config_set_reply_template", description="Sets the template for duplicate reply messages.")
async def config_set_reply_template(interaction: discord.Interaction, template: discord.Option(str, "The template string. See README for placeholders.")):
    """Sets the duplicate reply template."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    # Basic validation: Ensure it's a non-empty string
    if not template or not isinstance(template, str):
        await interaction.response.send_message("❌ Template cannot be empty.", ephemeral=True)
        return
    if len(template) > 1500: # Keep template reasonably short
        await interaction.response.send_message("❌ Template is too long (max 1500 chars).", ephemeral=True)
        return
    await _update_config(interaction, "duplicate_reply_template", template)
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
async def config_channel_add(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "The text channel to allow.")):
    """Adds a channel to the allowed list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') is None: guild_config['allowed_channel_ids'] = []
    if channel_id not in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].append(channel_id)
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Added {channel.mention}.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {channel.mention} already allowed.", ephemeral=True)

@bot.slash_command(name="config_channel_remove", description="Removes a channel from the allowed list.")
async def config_channel_remove(interaction: discord.Interaction, channel: discord.Option(discord.TextChannel, "The text channel to remove.")):
    """Removes a channel from the allowed list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    if guild_config.get('allowed_channel_ids') and channel_id in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].remove(channel_id);
        if not guild_config['allowed_channel_ids']: guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Removed {channel.mention}.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {channel.mention} not in list.", ephemeral=True)

@bot.slash_command(name="config_channel_clear", description="Clears the allowed channel list (monitors all).")
async def config_channel_clear(interaction: discord.Interaction):
    """Clears the allowed channel list."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy()
    if guild_config.get('allowed_channel_ids') is not None:
        guild_config['allowed_channel_ids'] = None
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message("✅ Cleared allowed channels.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save.", ephemeral=True)
    else: await interaction.response.send_message("ℹ️ Already monitoring all.", ephemeral=True)

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
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if user: mentions.append(f"- {user.mention} (`{user_id}`)")
            else: mentions.append(f"- *Unknown User* (`{user_id}`)")
        embed.description = '\n'.join(mentions); await interaction.response.send_message(embed=embed, ephemeral=True)
    else: await interaction.response.send_message("ℹ️ No users allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_add", description="Adds a user to the allowlist (exempt from checks).")
async def allowlist_add(interaction: discord.Interaction, user: discord.Option(discord.User, "The user to allowlist.")):
    """Adds a user to the allowlist."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    if 'allowed_users' not in guild_config or guild_config['allowed_users'] is None: guild_config['allowed_users'] = []
    if user_id not in guild_config['allowed_users']:
        guild_config['allowed_users'].append(user_id)
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Added {user.mention} to allowlist.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {user.mention} already allowlisted.", ephemeral=True)

@bot.slash_command(name="allowlist_remove", description="Removes a user from the allowlist.")
async def allowlist_remove(interaction: discord.Interaction, user: discord.Option(discord.User, "The user to remove from the allowlist.")):
    """Removes a user from the allowlist."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id).copy(); user_id = user.id
    if 'allowed_users' in guild_config and guild_config['allowed_users'] and user_id in guild_config['allowed_users']:
        guild_config['allowed_users'].remove(user_id)
        if await save_guild_config(guild_id, guild_config): await interaction.response.send_message(f"✅ Removed {user.mention} from allowlist.", ephemeral=True)
        else: await interaction.response.send_message(f"⚠️ Failed save.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ {user.mention} not allowlisted.", ephemeral=True)


# --- Hash Management Commands (Now Top-Level) ---

# Helper function to parse message link/ID remains the same
def parse_message_id(message_ref: str) -> int | None:
    match = re.search(r'/(\d+)$', message_ref);
    if match: return int(match.group(1))
    elif message_ref.isdigit(): return int(message_ref)
    return None

@bot.slash_command(name="hash_remove", description="Removes the stored hash associated with a specific message ID or link.")
async def remove_hash(interaction: discord.Interaction, message_reference: discord.Option(str, "The message ID or link containing the image hash to remove.")):
    """Removes a hash entry based on the original message ID."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()
    target_message_id = parse_message_id(message_reference)
    if target_message_id is None: await interaction.response.send_message("❌ Invalid message ID or link format.", ephemeral=True); return
    target_message_id_str = str(target_message_id); stored_hashes = await load_guild_hashes(guild_id, loop)
    hash_removed = False; key_to_remove = None; channel_key = None
    if not isinstance(stored_hashes, dict): await interaction.response.send_message("ℹ️ Hash DB empty/invalid.", ephemeral=True); return
    # Find and remove logic
    if current_scope == "server":
        for identifier in stored_hashes.keys():
            if identifier.startswith(target_message_id_str + "-"): key_to_remove = identifier; break
        if key_to_remove: del stored_hashes[key_to_remove]; hash_removed = True
    elif current_scope == "channel":
        for ch_id_str, channel_hashes in stored_hashes.items():
            if isinstance(channel_hashes, dict):
                for identifier in channel_hashes.keys():
                    if identifier.startswith(target_message_id_str + "-"): key_to_remove = identifier; channel_key = ch_id_str; break
            if key_to_remove: break
        if key_to_remove and channel_key:
            del stored_hashes[channel_key][key_to_remove]; hash_removed = True
            if not stored_hashes[channel_key]: del stored_hashes[channel_key]
    # Save and respond
    if hash_removed:
        if await save_guild_hashes(guild_id, stored_hashes, loop): await interaction.response.send_message(f"✅ Removed hash for msg `{target_message_id}`.", ephemeral=True)
        else: await interaction.response.send_message("⚠️ Error saving DB.", ephemeral=True)
    else: await interaction.response.send_message(f"ℹ️ No hash found for msg `{target_message_id}`.", ephemeral=True)

@bot.slash_command(name="hash_clear", description="Clears hashes for the server or a channel. Requires confirmation!")
async def clear_hashes(
        interaction: discord.Interaction,
        confirm: discord.Option(bool, "Must be True to confirm deletion.", required=True),
        channel: discord.Option(discord.TextChannel, "Optional: Specify a channel to clear hashes only for that channel (if scope is 'channel').", required=False, default=None)
):
    """Clears hashes with confirmation."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 **Confirmation required!** Set `confirm:True` to proceed.", ephemeral=True); return
    guild_id = interaction.guild_id; guild_config = get_guild_config(guild_id)
    current_scope = guild_config.get('duplicate_scope', 'server'); loop = asyncio.get_running_loop()
    target_desc = f"channel {channel.mention}" if channel else "the entire server"
    await interaction.response.defer(ephemeral=True)
    stored_hashes = await load_guild_hashes(guild_id, loop); cleared = False
    if not isinstance(stored_hashes, dict): await interaction.followup.send(f"ℹ️ Hash DB empty/invalid for {target_desc}.", ephemeral=True); return
    if channel: # Clear specific channel
        target_channel_id_str = str(channel.id)
        if current_scope == "channel":
            if target_channel_id_str in stored_hashes: del stored_hashes[target_channel_id_str]; cleared = True
            else: await interaction.followup.send(f"ℹ️ No hashes found for {channel.mention} (Scope is 'channel').", ephemeral=True); return
        elif current_scope == "server": await interaction.followup.send(f"ℹ️ Cannot clear specific channel when scope is 'server'. Use `/hash_clear confirm:True`.", ephemeral=True); return
    else: # Clear all for guild
        stored_hashes.clear(); cleared = True
    if cleared:
        if await save_guild_hashes(guild_id, stored_hashes, loop): await interaction.followup.send(f"✅ Cleared hashes for {target_desc}.", ephemeral=True)
        else: await interaction.followup.send(f"⚠️ Error saving cleared hash database.", ephemeral=True)


# --- Scan Command (Remains Top-Level) ---
@bot.slash_command(name="scan", description="Scans channel history to add/update image hashes.")
async def scan_history(
        interaction: discord.Interaction,
        channel: discord.Option(discord.TextChannel, "The channel to scan."),
        limit: discord.Option(int, "Max number of messages to scan.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT),
        flag_duplicates: discord.Option(bool, "Add reaction to non-oldest duplicates found?", default=False),
        reply_to_duplicates: discord.Option(bool, "Reply to non-oldest duplicates found?", default=False),
        delete_duplicates: discord.Option(bool, "Delete non-oldest duplicates found? (Requires Manage Messages)", default=False)
):
    """Scans past messages, populates DB (finds oldest), optionally flags/replies/deletes non-oldest duplicates."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return

    guild_id = interaction.guild_id; scan_channel_id = channel.id; scan_channel_id_str = str(scan_channel_id)
    guild_config = get_guild_config(guild_id); current_scope = guild_config.get('duplicate_scope', 'server')
    current_hash_size = guild_config.get('hash_size', 8); existence_threshold = 0
    current_mode = guild_config.get('duplicate_check_mode', 'strict')
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    current_duration = guild_config.get('duplicate_check_duration_days', 0)
    reply_template = guild_config.get('duplicate_reply_template', DEFAULT_REPLY_TEMPLATE) # Get template for replies

    # Check permissions
    bot_member = interaction.guild.me
    if not channel.permissions_for(bot_member).read_message_history:
        await interaction.response.send_message(f"❌ Bot lacks 'Read Message History' in {channel.mention}.", ephemeral=True); return
    perms_needed = []
    if flag_duplicates and not channel.permissions_for(bot_member).add_reactions: perms_needed.append("Add Reactions")
    if reply_to_duplicates and not channel.permissions_for(bot_member).send_messages: perms_needed.append("Send Messages")
    if delete_duplicates and not channel.permissions_for(bot_member).manage_messages: perms_needed.append("Manage Messages")
    if perms_needed:
        await interaction.response.send_message(f"❌ Bot lacks permissions ({', '.join(perms_needed)}) in {channel.mention} for requested actions.", ephemeral=True); return

    await interaction.response.defer(ephemeral=False)
    status_message = await interaction.followup.send(f"⏳ Starting scan: {channel.mention}, limit={limit}, Flagging:{flag_duplicates}, Replying:{reply_to_duplicates}, Deleting:{delete_duplicates}...", wait=True)

    processed_messages = 0; added_hashes = 0; updated_hashes = 0; flagged_count = 0; replied_count = 0; deleted_count = 0
    skipped_attachments = 0; errors = 0
    loop = asyncio.get_running_loop()

    # --- Phase 1: Gather all image data ---
    scanned_image_data = defaultdict(list)
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1: Gathering image data...")
    try:
        async for message in channel.history(limit=limit):
            processed_messages += 1
            if processed_messages % SCAN_UPDATE_INTERVAL == 0:
                try: await status_message.edit(content=f"⏳ Scanning... Processed {processed_messages}/{limit} (Phase 1).")
                except Exception as e: print(f"DEBUG: Error editing status: {e}")
            if message.author.bot or not message.attachments: continue
            message_user_id = message.author.id; message_timestamp = message.created_at.replace(tzinfo=datetime.timezone.utc)
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    try:
                        image_bytes = await attachment.read(); img_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                        if img_hash is None: skipped_attachments += 1; continue
                        scanned_image_data[str(img_hash)].append((message_timestamp, message.id, message_user_id, attachment.filename, message))
                    except discord.HTTPException as e: print(f"Warning: [Scan G:{guild_id}] Failed download {attachment.id}: {e}"); errors += 1; skipped_attachments += 1
                    except Exception as e: print(f"Error: [Scan G:{guild_id}] Error processing attach {attachment.id}: {e}"); errors += 1; skipped_attachments += 1; traceback.print_exc()
    except discord.Forbidden: await status_message.edit(content=f"❌ Scan failed (Phase 1). Bot lacks permissions in {channel.mention}."); return
    except Exception as e: await status_message.edit(content=f"❌ Error during scan (Phase 1): {e}"); traceback.print_exc(); return
    print(f"DEBUG: [Scan G:{guild_id}] Phase 1 complete. Found {len(scanned_image_data)} unique hash groups.")

    # --- Phase 2: Process hashes ---
    await status_message.edit(content=f"⏳ Processing {len(scanned_image_data)} unique hash groups...")
    stored_hashes = await load_guild_hashes(guild_id, loop); db_updated = False; processed_hashes = 0
    for img_hash_str, entries in scanned_image_data.items():
        processed_hashes += 1
        if processed_hashes % SCAN_UPDATE_INTERVAL == 0:
            try: await status_message.edit(content=f"⏳ Processing... {processed_hashes}/{len(scanned_image_data)} hashes. Added:{added_hashes} Updated:{updated_hashes} Replied:{replied_count} Flagged:{flagged_count} Deleted:{deleted_count}")
            except Exception as e: print(f"DEBUG: Error editing status: {e}")
        if not entries: continue
        entries.sort(key=lambda x: x[0]); oldest_entry = entries[0]
        oldest_timestamp, oldest_msg_id, oldest_user_id, oldest_filename, oldest_message_obj = oldest_entry
        oldest_identifier = f"{oldest_msg_id}-{oldest_filename}"
        oldest_hash_data = {"hash": img_hash_str, "user_id": oldest_user_id, "timestamp": oldest_timestamp.isoformat()}
        img_hash_obj = imagehash.hex_to_hash(img_hash_str)
        existing_identifier, existing_data = find_existing_hash_entry_sync(img_hash_obj, stored_hashes, existence_threshold, current_scope, scan_channel_id_str)
        update_needed = False; identifier_to_use = oldest_identifier; data_to_use = oldest_hash_data
        if existing_identifier:
            existing_timestamp_str = None
            if isinstance(existing_data, dict): existing_timestamp_str = existing_data.get('timestamp')
            if existing_timestamp_str:
                try:
                    existing_time = dateutil.parser.isoparse(existing_timestamp_str)
                    if existing_time.tzinfo is None: existing_time = existing_time.replace(tzinfo=datetime.timezone.utc)
                    if oldest_timestamp < existing_time: update_needed = True; updated_hashes += 1
                    else: identifier_to_use = existing_identifier; data_to_use = existing_data if isinstance(existing_data, dict) else {"hash": existing_data, "user_id": None, "timestamp": None}
                except Exception: pass
        else: update_needed = True; added_hashes += 1
        # DB Update/Add
        if update_needed:
            db_updated = True
            if current_scope == "server":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                if existing_identifier and existing_identifier != identifier_to_use: stored_hashes.pop(existing_identifier, None)
                stored_hashes[identifier_to_use] = data_to_use
            elif current_scope == "channel":
                if not isinstance(stored_hashes, dict): stored_hashes = {}
                channel_hashes = stored_hashes.setdefault(scan_channel_id_str, {})
                if not isinstance(channel_hashes, dict): channel_hashes = {}; stored_hashes[scan_channel_id_str] = channel_hashes
                if existing_identifier and existing_identifier != identifier_to_use: channel_hashes.pop(existing_identifier, None)
                channel_hashes[identifier_to_use] = data_to_use
        # Flagging/Replying/Deleting Logic
        for entry_timestamp, entry_msg_id, entry_user_id, entry_filename, entry_message_obj in entries:
            if entry_msg_id == oldest_msg_id: continue
            is_violation = False
            if current_mode == "strict": is_violation = True
            elif current_mode == "owner_allowed":
                if oldest_user_id is None or oldest_user_id != entry_user_id: is_violation = True
            if is_violation and current_duration > 0:
                if (entry_timestamp - oldest_timestamp).days > current_duration: is_violation = False
            if is_violation:
                action_taken = False
                # Reply
                if reply_to_duplicates:
                    try:
                        # Format reply using template
                        template_data = {
                            "mention": f"<@{entry_user_id}>", # Mention the user of *this* message
                            "filename": entry_filename,
                            "identifier": oldest_identifier, # Reference the oldest
                            "distance": "?", # Distance isn't easily available here, maybe omit or calculate if needed
                            "original_user_mention": f"<@{oldest_user_id}>" if oldest_user_id else "*Unknown*",
                            "emoji": duplicate_reaction_emoji,
                            "original_user_info": f", Orig User: <@{oldest_user_id}>" if oldest_user_id else "",
                            "jump_link": ""
                        }
                        if oldest_message_obj:
                            try:
                                jump_url = oldest_message_obj.jump_url
                                template_data["jump_link"] = f"\nOriginal: {jump_url}"
                            except: pass
                        reply_text = reply_template.format_map(defaultdict(str, template_data))

                        await entry_message_obj.reply(reply_text, mention_author=False)
                        replied_count += 1; action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Send Messages perm.")
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for reply.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed reply to {entry_msg_id}: {e}")
                # React
                if flag_duplicates:
                    try:
                        already_reacted = False; refreshed_message = await channel.fetch_message(entry_message_obj.id)
                        for reaction in refreshed_message.reactions:
                            if str(reaction.emoji) == duplicate_reaction_emoji and reaction.me: already_reacted = True; break
                        if not already_reacted: await entry_message_obj.add_reaction(duplicate_reaction_emoji); flagged_count += 1; action_taken = True
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Add Reactions perm."); break
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for reaction.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed reaction: {e}")
                # Delete
                if delete_duplicates:
                    try: await entry_message_obj.delete(); deleted_count += 1; action_taken = True; print(f"DEBUG: [Scan Action G:{guild_id}] Deleted msg {entry_msg_id}")
                    except discord.Forbidden: print(f"Warning: [Scan Action G:{guild_id}] Missing Manage Messages perm.")
                    except discord.NotFound: print(f"Warning: [Scan Action G:{guild_id}] Msg {entry_msg_id} not found for deletion.")
                    except Exception as e: print(f"DEBUG: [Scan Action G:{guild_id}] Failed delete msg {entry_msg_id}: {e}")
                # Delay
                if action_taken: await asyncio.sleep(SCAN_ACTION_DELAY)
    # Final Save and Report
    if db_updated:
        print(f"DEBUG: [Scan G:{guild_id}] Saving updated hash database after scan...")
        if not await save_guild_hashes(guild_id, stored_hashes, loop):
            try: await interaction.followup.send("⚠️ Error saving hashes after scan.", ephemeral=True)
            except: pass
    await status_message.edit(content=f"✅ Scan Complete! Processed {processed_messages}. Added:{added_hashes}, Updated:{updated_hashes}, Replied:{replied_count}, Flagged:{flagged_count}, Deleted:{deleted_count}. Skipped:{skipped_attachments}. Errors:{errors}.")


# --- New Clear Flags Command ---
@bot.slash_command(name="clearflags", description="Removes the bot's duplicate warning reactions from messages in a channel.")
async def clear_flags(
        interaction: discord.Interaction,
        channel: discord.Option(discord.TextChannel, "The channel to clear reactions from."),
        confirm: discord.Option(bool, "Must be True to confirm clearing reactions.", required=True), # Required comes first
        limit: discord.Option(int, "Max number of messages to check.", min_value=1, max_value=10000, default=DEFAULT_SCAN_LIMIT) # Optional comes last
):
    """Removes the bot's configured duplicate reaction from messages in channel history."""
    if not interaction.guild_id: await interaction.response.send_message("❌ Server only.", ephemeral=True); return
    if not await check_admin_permissions(interaction): return
    if not confirm: await interaction.response.send_message(f"🛑 **Confirmation required!** Set `confirm:True` to proceed.", ephemeral=True); return

    guild_id = interaction.guild_id
    guild_config = get_guild_config(guild_id)
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')
    bot_member = interaction.guild.me # Get the bot's Member object in this guild

    # Check permissions
    if not channel.permissions_for(bot_member).read_message_history:
        await interaction.response.send_message(f"❌ Bot lacks 'Read Message History' in {channel.mention}.", ephemeral=True); return
    if not channel.permissions_for(bot_member).add_reactions: # Need Add Reactions to remove own reactions
        print(f"Warning: [ClearFlags G:{guild_id}] Bot might lack Add Reactions permission needed to remove reactions in {channel.mention}.")
        # Proceed anyway, try/except will handle actual failures

    await interaction.response.defer(ephemeral=False) # Public deferral
    status_message = await interaction.followup.send(f"⏳ Starting reaction cleanup in {channel.mention} (limit {limit})...", wait=True)

    processed_msgs = 0
    reactions_removed = 0
    errors = 0

    try:
        async for message in channel.history(limit=limit):
            processed_msgs += 1
            if processed_msgs % SCAN_UPDATE_INTERVAL == 0:
                try: await status_message.edit(content=f"⏳ Clearing flags... Checked {processed_msgs}/{limit}. Removed {reactions_removed}.")
                except Exception as e: print(f"DEBUG: Error editing status: {e}")

            # Check reactions on the message
            for reaction in message.reactions:
                # Is it the configured emoji AND was it added by the bot itself?
                if str(reaction.emoji) == duplicate_reaction_emoji and reaction.me:
                    try:
                        # Use bot_member variable here
                        await message.remove_reaction(duplicate_reaction_emoji, bot_member)
                        reactions_removed += 1
                        await asyncio.sleep(CLEAR_REACTION_DELAY) # Delay between removals
                    except discord.Forbidden:
                        print(f"Warning: [ClearFlags G:{guild_id}] Missing permission to remove reaction in {channel.mention}.")
                        # Stop trying if permission denied once? Or keep trying per message? Keep trying for now.
                    except discord.NotFound:
                        print(f"Warning: [ClearFlags G:{guild_id}] Message {message.id} or reaction not found.")
                    except Exception as e:
                        print(f"Error: [ClearFlags G:{guild_id}] Failed to remove reaction from {message.id}: {e}")
                        errors += 1
                    break # Move to next message once bot's reaction is found/handled

    except discord.Forbidden: await status_message.edit(content=f"❌ Cleanup failed. Bot lacks permissions in {channel.mention}."); return
    except Exception as e: await status_message.edit(content=f"❌ Error during cleanup: {e}"); traceback.print_exc(); return

    await status_message.edit(content=f"✅ Cleanup Complete! Checked {processed_msgs} messages in {channel.mention}. Removed **{reactions_removed}** reactions. Errors: {errors}.")


# --- Main Execution ---
if __name__ == "__main__":
    try: import dateutil.parser
    except ImportError: print("Optional dependency 'python-dateutil' not found. Timestamps might not parse correctly. Consider: pip install python-dateutil")
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found.", file=sys.stderr); sys.exit(1)
    try: print("Starting bot..."); bot.run(BOT_TOKEN)
    except Exception as e: print(f"An error occurred: {e}", file=sys.stderr); traceback.print_exc()
    finally: print("DEBUG: Bot run loop finished or encountered an error.")


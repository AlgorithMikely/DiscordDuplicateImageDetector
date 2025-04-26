#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot to detect duplicate or near-duplicate images posted in channels
using perceptual hashing. Supports per-server configuration and hash databases.
Duplicate detection scope can be set per-server ('server' or 'channel').
Duplicate check mode can be set per-server ('strict' or 'owner_allowed').
Loads token from .env file. Settings managed via Discord commands.
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
CONFIG_FILE_PATH = 'server_configs.json' # Stores configs for all guilds
DEFAULT_COMMAND_PREFIX = "!"
HASH_FILENAME_PREFIX = "hashes_" # Prefix for per-guild hash files
VALID_SCOPES = ["server", "channel"]
VALID_CHECK_MODES = ["strict", "owner_allowed"]

# --- Load Environment Variables ---
load_dotenv()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# --- Global Configuration Cache & Locks ---
server_configs = {} # Holds {guild_id: config_dict, ...}
config_lock = asyncio.Lock() # Lock for accessing/saving the main config file
hash_file_locks = {} # {guild_id: asyncio.Lock()}

# --- Configuration Loading/Saving ---

def get_default_guild_config(guild_id):
    """Returns a dictionary with default settings for a guild."""
    return {
        "hash_db_file": f"{HASH_FILENAME_PREFIX}{guild_id}.json",
        "hash_size": 8,
        "similarity_threshold": 5,
        "allowed_channel_ids": None,
        "react_to_duplicates": True,
        "delete_duplicates": False,
        "duplicate_reaction_emoji": "⚠️",
        "duplicate_scope": "server", # Default scope
        "duplicate_check_mode": "strict" # Default check mode
    }

def validate_config_data(config_data):
     """Validates config, including new check_mode."""
     # Start with defaults and update with loaded data to ensure all keys exist
     validated = get_default_guild_config(0).copy() # Use 0 as placeholder ID for defaults
     validated.update(config_data)
     try:
        # Coerce types
        validated['hash_size'] = int(validated['hash_size'])
        validated['similarity_threshold'] = int(validated['similarity_threshold'])
        validated['react_to_duplicates'] = bool(validated['react_to_duplicates'])
        validated['delete_duplicates'] = bool(validated['delete_duplicates'])

        # Validate scope
        if validated.get('duplicate_scope') not in VALID_SCOPES:
             print(f"Warning: Invalid 'duplicate_scope' value '{validated.get('duplicate_scope')}' found. Resetting to 'server'.", file=sys.stderr)
             validated['duplicate_scope'] = "server"

        # Validate check mode
        if validated.get('duplicate_check_mode') not in VALID_CHECK_MODES:
             print(f"Warning: Invalid 'duplicate_check_mode' value '{validated.get('duplicate_check_mode')}' found. Resetting to 'strict'.", file=sys.stderr)
             validated['duplicate_check_mode'] = "strict"

        # Validate allowed_channel_ids (must be list of ints or None)
        if validated['allowed_channel_ids'] is not None:
            if isinstance(validated['allowed_channel_ids'], list):
                 # Filter out non-integer elements and convert valid ones
                 validated['allowed_channel_ids'] = [int(ch_id) for ch_id in validated['allowed_channel_ids'] if str(ch_id).isdigit()]
                 # If list becomes empty after filtering, set to None
                 if not validated['allowed_channel_ids']:
                      validated['allowed_channel_ids'] = None
            else:
                 # If it's not None and not a list, reset to None
                 print(f"Warning: 'allowed_channel_ids' was not a list. Resetting to None.", file=sys.stderr)
                 validated['allowed_channel_ids'] = None

     except (ValueError, TypeError, KeyError) as e:
          # Catch potential errors during type coercion or key access
          print(f"Warning: Error validating config types or keys: {e}. Some defaults may be used.", file=sys.stderr)
          # You might want more robust error handling here, e.g., resetting specific keys
          pass
     return validated

async def load_main_config():
    """Loads the main server_configs.json file into the global cache."""
    global server_configs
    async with config_lock:
        print(f"DEBUG: Loading main config file: {CONFIG_FILE_PATH}")
        try:
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                if not isinstance(loaded_data, dict):
                     print(f"Error: Main config file {CONFIG_FILE_PATH} is not a JSON object. Using empty config.", file=sys.stderr)
                     server_configs = {}
                     return

                # Validate each guild's config within the loaded data
                validated_configs = {}
                for guild_id_str, guild_config_data in loaded_data.items():
                    try:
                        guild_id = int(guild_id_str)
                        # Validate the loaded data for this guild
                        validated_configs[guild_id] = validate_config_data(guild_config_data)
                        # Ensure hash_db_file name is consistent with guild_id
                        validated_configs[guild_id]['hash_db_file'] = f"{HASH_FILENAME_PREFIX}{guild_id}.json"
                    except ValueError:
                        print(f"Warning: Invalid guild ID '{guild_id_str}' in config file. Skipping.", file=sys.stderr)
                server_configs = validated_configs
                print(f"Successfully loaded configurations for {len(server_configs)} guilds.")

        except FileNotFoundError:
            print(f"Info: Config file '{CONFIG_FILE_PATH}' not found. Will be created on first save.", file=sys.stderr)
            server_configs = {} # Start with empty config if file doesn't exist
        except json.JSONDecodeError as e:
            print(f"Error: Could not decode JSON from '{CONFIG_FILE_PATH}'. Check format. Using empty config. Error: {e}", file=sys.stderr)
            server_configs = {}
        except Exception as e:
            print(f"Error loading main config file '{CONFIG_FILE_PATH}': {e}. Using empty config.", file=sys.stderr)
            traceback.print_exc()
            server_configs = {}

async def save_main_config():
    """Saves the global server_configs cache to server_configs.json."""
    async with config_lock:
        print(f"DEBUG: Saving main config file: {CONFIG_FILE_PATH}")
        # Convert guild_id keys back to strings for JSON compatibility
        config_to_save = {str(gid): data for gid, data in server_configs.items()}
        try:
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=4)
            print(f"DEBUG: Successfully saved main config for {len(config_to_save)} guilds.")
            return True
        except IOError as e:
            print(f"DEBUG: Error - Could not write to main config file '{CONFIG_FILE_PATH}': {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"DEBUG: Error saving main config file '{CONFIG_FILE_PATH}': {e}", file=sys.stderr)
            traceback.print_exc()
            return False

def get_guild_config(guild_id):
    """Gets guild config, ensures defaults exist (incl. new fields)."""
    global server_configs
    defaults_needed = False
    # If guild not in cache, create its default config
    if guild_id not in server_configs:
        print(f"DEBUG: No config found for guild {guild_id}. Creating defaults.")
        server_configs[guild_id] = get_default_guild_config(guild_id)
        defaults_needed = True
    else:
        # If guild exists, check if it has all the latest default keys
        guild_conf = server_configs[guild_id]
        default_conf = get_default_guild_config(guild_id)
        updated = False
        for key, default_value in default_conf.items():
            if key not in guild_conf:
                 print(f"DEBUG: Adding missing default key '{key}' for guild {guild_id}.")
                 guild_conf[key] = default_value
                 updated = True
        # If keys were added, re-validate the whole config dict for the guild
        if updated:
             server_configs[guild_id] = validate_config_data(guild_conf)
             defaults_needed = True # Mark that save is needed

    # If defaults were created or added, schedule a save
    if defaults_needed:
        # Use asyncio.create_task to run save_main_config without blocking
        asyncio.create_task(save_main_config())

    return server_configs[guild_id]

async def save_guild_config(guild_id, guild_config_data):
     """Updates a specific guild's config in the cache and saves the main file."""
     global server_configs
     # Validate before saving
     server_configs[guild_id] = validate_config_data(guild_config_data)
     # Ensure hash_db_file name is consistent
     server_configs[guild_id]['hash_db_file'] = f"{HASH_FILENAME_PREFIX}{guild_id}.json"
     return await save_main_config()


# --- Hashing and File I/O Functions ---

def get_hash_file_lock(guild_id):
    """Gets or creates the asyncio.Lock for a specific guild's hash file."""
    global hash_file_locks
    if guild_id not in hash_file_locks:
        hash_file_locks[guild_id] = asyncio.Lock()
    return hash_file_locks[guild_id]

def calculate_hash_sync(image_bytes, hash_size):
    """Synchronous hash calculation."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Using dhash (difference hash)
        return imagehash.dhash(img, hash_size=hash_size)
    except UnidentifiedImageError:
        print("DEBUG: Error - Cannot identify image file format from bytes.", file=sys.stderr)
        return None
    except Exception as e:
        # Log other potential image processing errors
        print(f"DEBUG: Error processing image from bytes: {e}", file=sys.stderr)
        traceback.print_exc()
        return None

async def calculate_hash(image_bytes, hash_size, loop):
    """Calculates the perceptual hash asynchronously using an executor."""
    func = partial(calculate_hash_sync, image_bytes, hash_size)
    # Run the synchronous function in the default thread pool executor
    hash_value = await loop.run_in_executor(None, func)
    return hash_value

def load_hashes_sync(db_file):
    """Synchronous hash loading from a specific file."""
    if not os.path.exists(db_file):
        return {} # Return empty if file doesn't exist
    try:
        with open(db_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Basic validation: ensure it's a dictionary
        if not isinstance(data, dict):
            print(f"Warning: Hash file '{db_file}' content is not a dictionary. Returning empty.", file=sys.stderr)
            return {}

        # Check format (best effort) - Helps warn about old format data
        is_new_format_likely = False
        if data: # Only check if data is not empty
            # Check top level values or nested values for the new structure
            for v in data.values():
                if isinstance(v, dict) and 'hash' in v and 'user_id' in v:
                    is_new_format_likely = True; break
                if isinstance(v, dict): # Check nested dict for channel scope
                    for subv in v.values():
                        if isinstance(subv, dict) and 'hash' in subv and 'user_id' in subv:
                            is_new_format_likely = True; break
                if is_new_format_likely: break

        if not is_new_format_likely and data:
             print(f"Warning: Hash file '{db_file}' might be in old format (expected {{'hash': ..., 'user_id': ...}}). Owner checks may be unreliable.", file=sys.stderr)

        # print(f"DEBUG: Loaded hashes from '{db_file}'.") # Can be noisy
        return data
    except json.JSONDecodeError as e:
        print(f"DEBUG: Error decoding JSON from hash db '{db_file}': {e}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"DEBUG: Error loading hash db '{db_file}': {e}", file=sys.stderr)
        traceback.print_exc()
        return {}

def save_hashes_sync(hashes_dict, db_file):
    """Synchronous hash saving to a specific file."""
    try:
        with open(db_file, 'w', encoding='utf-8') as f:
            json.dump(hashes_dict, f, indent=4)
        # print(f"DEBUG: Saved hashes to '{db_file}'.") # Can be noisy
        return True
    except IOError as e:
         print(f"DEBUG: Error - Could not write to hash file '{db_file}': {e}", file=sys.stderr)
         return False
    except Exception as e:
        print(f"DEBUG: Error saving hash db '{db_file}': {e}", file=sys.stderr)
        traceback.print_exc()
        return False

async def load_guild_hashes(guild_id, loop):
    """Loads the hash database for a specific guild using its lock."""
    guild_config = get_guild_config(guild_id)
    db_file = guild_config['hash_db_file']
    lock = get_hash_file_lock(guild_id)
    async with lock:
        # print(f"DEBUG: Loading hashes for guild {guild_id} from {db_file}...") # Can be noisy
        func = partial(load_hashes_sync, db_file)
        hashes_dict = await loop.run_in_executor(None, func)
    return hashes_dict

async def save_guild_hashes(guild_id, hashes_dict, loop):
    """Saves the hash database for a specific guild using its lock."""
    guild_config = get_guild_config(guild_id)
    db_file = guild_config['hash_db_file']
    lock = get_hash_file_lock(guild_id)
    async with lock:
        # print(f"DEBUG: Saving hashes for guild {guild_id} to {db_file}...") # Can be noisy
        func = partial(save_hashes_sync, hashes_dict, db_file)
        success = await loop.run_in_executor(None, func)
    return success

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
            # Get the specific channel's hash dict, default to empty if channel not found
            channel_data = stored_hashes_dict.get(channel_id_str, {})
            # Ensure the channel data itself is a dictionary
            if isinstance(channel_data, dict): hashes_to_check = channel_data
    else:
        print(f"Error: Unknown scope '{scope}' in find_duplicates_sync.", file=sys.stderr)
        return duplicates # Unknown scope

    # Iterate through the selected hashes
    for identifier, hash_data in hashes_to_check.items():
        stored_hash_str = None
        original_user_id = None # Default to None

        # Determine hash string and user ID based on stored format
        if isinstance(hash_data, dict) and 'hash' in hash_data: # New format
            stored_hash_str = hash_data.get('hash')
            original_user_id = hash_data.get('user_id') # Get user_id if present
        elif isinstance(hash_data, str): # Old format (assume it's just the hash string)
            stored_hash_str = hash_data
            # original_user_id remains None

        # Skip if we couldn't extract a hash string
        if stored_hash_str is None: continue

        try:
            # Compare hashes
            stored_hash = imagehash.hex_to_hash(stored_hash_str)
            distance = new_image_hash - stored_hash
            # Check if within threshold
            if distance <= threshold:
                original_message_id = None
                # Try to extract original message ID from identifier (best effort)
                try: original_message_id = int(identifier.split('-')[0])
                except: pass # Ignore if format is different or extraction fails
                # Append match data including original user ID
                duplicates.append({
                    'identifier': identifier,
                    'distance': distance,
                    'original_message_id': original_message_id,
                    'original_user_id': original_user_id # Will be None if not found/old format
                })
        except ValueError:
             # Silently ignore errors converting hex string to hash
             pass
        except Exception as e:
            # Log other comparison errors
            print(f"DEBUG: Error comparing hash for identifier '{identifier}': {e}", file=sys.stderr)

    # Sort matches by distance (closest first)
    duplicates.sort(key=lambda x: x['distance'])
    return duplicates

async def find_duplicates(new_image_hash, stored_hashes_dict, threshold, scope, channel_id, loop):
    """Async wrapper for duplicate finding."""
    # Pass necessary arguments to the synchronous function
    func = partial(find_duplicates_sync, new_image_hash, stored_hashes_dict, threshold, scope, str(channel_id))
    duplicates = await loop.run_in_executor(None, func)
    return duplicates


# --- Discord Bot Implementation ---
# Define necessary intents for bot operation
intents = discord.Intents.default()
intents.messages = True          # To receive message events
intents.message_content = True   # To read message content and attachments (PRIVILEGED)
intents.guilds = True            # To access guild information (members, channels)
intents.reactions = True         # To add reactions

# Use commands.Bot for command handling
bot = commands.Bot(command_prefix=DEFAULT_COMMAND_PREFIX, intents=intents)

# --- Event Handlers ---
@bot.event
async def on_ready():
    """Called when the bot logs in and is ready."""
    print(f'--- Logged in as {bot.user.name} (ID: {bot.user.id}) ---')
    print(f'--- Command Prefix: {bot.command_prefix} ---')
    # Load configurations for all guilds the bot is currently in
    await load_main_config()
    print(f'--- Ready for {len(bot.guilds)} guilds ---')
    # Ensure config entries exist for all guilds upon ready
    for guild in bot.guilds:
         _ = get_guild_config(guild.id) # This call ensures defaults are created if needed
    print('------')

@bot.event
async def on_guild_join(guild):
     """Called when the bot joins a new guild."""
     print(f"Joined new guild: {guild.name} (ID: {guild.id})")
     # Ensure a default config exists for this new guild
     _ = get_guild_config(guild.id)
     # Save the main config file immediately to persist the new entry
     await save_main_config()

@bot.event
async def on_message(message):
    """Handles incoming messages for image processing and duplicate checks."""
    # Ignore messages from DMs, self, or other bots
    if message.guild is None or message.author == bot.user or message.author.bot:
        return

    # Allow commands library to process potential commands first
    await bot.process_commands(message)

    # Check if the message was actually processed as a valid command
    ctx = await bot.get_context(message)
    # If it was a command, don't proceed with image checking for this message
    if ctx.valid:
        return

    # --- Image Processing Logic (Only if it wasn't a command) ---
    guild_id = message.guild.id
    channel_id = message.channel.id
    channel_id_str = str(channel_id) # Use string for JSON keys if needed later
    current_user_id = message.author.id
    # Get the configuration specific to this guild
    guild_config = get_guild_config(guild_id)

    # Extract relevant settings from the guild's config
    allowed_channel_ids = guild_config.get('allowed_channel_ids')
    current_scope = guild_config.get('duplicate_scope', 'server') # Default to server
    current_mode = guild_config.get('duplicate_check_mode', 'strict') # Default to strict
    current_hash_size = guild_config.get('hash_size', 8) # Default hash size
    current_similarity_threshold = guild_config.get('similarity_threshold', 5) # Default threshold
    react_to_duplicates = guild_config.get('react_to_duplicates', True)
    delete_duplicates = guild_config.get('delete_duplicates', False)
    duplicate_reaction_emoji = guild_config.get('duplicate_reaction_emoji', '⚠️')

    # Check if processing should happen in this channel
    if allowed_channel_ids and channel_id not in allowed_channel_ids:
        return # Silently ignore if channel is not allowed

    # Check if there are attachments to process
    if not message.attachments:
        return

    # print(f"DEBUG: [G:{guild_id} C:{channel_id}] Msg with attachments received.") # Less verbose

    loop = asyncio.get_running_loop()
    # Load the hash database specific to this guild
    stored_hashes = await load_guild_hashes(guild_id, loop)
    db_updated = False # Flag to track if the hash DB needs saving

    # Process each attachment
    for i, attachment in enumerate(message.attachments):
        # Check if the attachment is an image
        if attachment.content_type and attachment.content_type.startswith('image/'):
            try:
                # Download image bytes
                image_bytes = await attachment.read()
                # Calculate its hash
                new_hash = await calculate_hash(image_bytes, current_hash_size, loop)
                # Skip if hashing failed
                if new_hash is None: continue

                # Find potential duplicates based on scope
                duplicate_matches = await find_duplicates(
                    new_hash, stored_hashes, current_similarity_threshold,
                    current_scope, channel_id, loop
                )

                is_violation = False
                violating_match = None # Store the specific match that caused the violation

                # Determine if a violation occurred based on check mode
                if duplicate_matches:
                    if current_mode == "strict":
                        # Any match is a violation in strict mode
                        is_violation = True
                        violating_match = duplicate_matches[0] # Use the closest match for reporting
                        print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:Strict] Duplicate Found!")
                    elif current_mode == "owner_allowed":
                        # Check if any match is from a *different* user or has unknown owner
                        for match in duplicate_matches:
                            original_owner_id = match.get('original_user_id')
                            # Violation if owner unknown (old format) or different from current user
                            if original_owner_id is None or original_owner_id != current_user_id:
                                is_violation = True
                                violating_match = match # Use the first violating match
                                print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:OwnerAllowed] Duplicate Found (Orig User: {original_owner_id}, Curr User: {current_user_id})")
                                break # Stop checking once a violation is found
                        if not is_violation:
                             # If loop finished without finding a violation, it means all matches were owned by current user
                             print(f"DEBUG: [G:{guild_id} Scope:{current_scope} Mode:OwnerAllowed] Duplicate Found, but current user is owner. Allowing.")

                # --- Handle Violation (if any) ---
                if is_violation and violating_match:
                    # Extract details from the violating match
                    identifier = violating_match['identifier']
                    distance = violating_match['distance']
                    original_message_id = violating_match.get('original_message_id')
                    original_user_id = violating_match.get('original_user_id')

                    # Construct reply message
                    reply_text = (
                        f"{duplicate_reaction_emoji} Hold on, {message.author.mention}! Image `{attachment.filename}` is similar "
                        f"to a prior submission (ID: `{identifier}`, Dist: {distance}"
                    )
                    # Mention original user if known
                    if original_user_id:
                         reply_text += f", Orig User: <@{original_user_id}>"
                    reply_text += ")."

                    # Add jump URL if possible
                    if original_message_id and message.guild:
                         try:
                            # Note: Link might point to a different channel if scope is 'server'
                            jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{original_message_id}"
                            reply_text += f"\nOriginal might be here: {jump_url}"
                         except Exception: pass # Ignore errors creating jump url

                    # Send the reply
                    await message.reply(reply_text, mention_author=True)

                    # Perform configured actions (react/delete)
                    if react_to_duplicates:
                        try: await message.add_reaction(duplicate_reaction_emoji)
                        except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed reaction: {e}")
                    if delete_duplicates:
                        # Ensure bot has delete permissions before attempting
                        if message.channel.permissions_for(message.guild.me).manage_messages:
                             try: await message.delete()
                             except Exception as e: print(f"DEBUG: [G:{guild_id}] Failed delete: {e}")
                        else: print(f"DEBUG: [G:{guild_id}] Lacking 'Manage Messages' permission to delete.")


                # --- Add Unique Hash (if no violation occurred) ---
                # Add if no matches were found OR if mode is owner_allowed and no violation occurred
                elif not is_violation:
                    print(f"DEBUG: [G:{guild_id} Scope:{current_scope}] Image '{attachment.filename}' unique or allowed repost. Adding.")
                    unique_identifier = f"{message.id}-{attachment.filename}"
                    # Store in new format: { "hash": "...", "user_id": ... }
                    hash_data_to_store = {"hash": str(new_hash), "user_id": current_user_id}

                    # Add the hash data based on the current scope
                    if current_scope == "server":
                        # Ensure root level is a dict
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        stored_hashes[unique_identifier] = hash_data_to_store
                    elif current_scope == "channel":
                        # Ensure root level is a dict
                        if not isinstance(stored_hashes, dict): stored_hashes = {}
                        # Get or create the dict for this specific channel
                        channel_hashes = stored_hashes.setdefault(channel_id_str, {})
                        # Ensure the channel data is actually a dict (safety check)
                        if not isinstance(channel_hashes, dict):
                            channel_hashes = {}
                            stored_hashes[channel_id_str] = channel_hashes
                        # Add the hash data to the channel's dict
                        channel_hashes[unique_identifier] = hash_data_to_store

                    db_updated = True # Mark that the hash DB needs saving

            except discord.HTTPException as e:
                 # Handle potential errors downloading the attachment
                 print(f"DEBUG: [G:{guild_id}] HTTP Error processing attachment '{attachment.filename}': {e}", file=sys.stderr)
            except Exception as e:
                # Catch any other unexpected errors during processing
                print(f"DEBUG: [G:{guild_id}] Error processing attachment '{attachment.filename}': {e}", file=sys.stderr)
                traceback.print_exc()

    # Save the hash database if any new hashes were added
    if db_updated:
        print(f"DEBUG: [G:{guild_id}] Saving updated hash database...")
        await save_guild_hashes(guild_id, stored_hashes, loop)


# --- Configuration Commands (Scope/Mode Aware) ---

# Decorators must be on separate lines
@bot.group(name="config", invoke_without_command=True)
@commands.guild_only() # Ensure command is run in a server
@commands.has_permissions(administrator=True) # Check for admin permissions
async def configcmd(ctx):
    """Base command shows current server settings."""
    guild_id = ctx.guild.id
    guild_config = get_guild_config(guild_id) # Get config for the command's guild

    embed = discord.Embed(title=f"Bot Configuration for {ctx.guild.name}", color=discord.Color.blue())

    # Display scope and mode prominently
    scope = guild_config.get('duplicate_scope', 'server')
    mode = guild_config.get('duplicate_check_mode', 'strict')
    embed.add_field(name="Duplicate Scope", value=f"`{scope}`", inline=True)
    embed.add_field(name="Duplicate Check Mode", value=f"`{mode}`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True) # Invisible spacer field

    # Display other settings
    for key, value in guild_config.items():
        # Skip keys already displayed
        if key in ['duplicate_scope', 'duplicate_check_mode']: continue

        display_value = value # Default display
        # Format specific keys for better readability
        if key == 'allowed_channel_ids':
            display_value = ', '.join(f'<#{ch_id}>' for ch_id in value) if value else "All Channels"
        elif key == 'hash_db_file':
             display_value = f"`{value}`" # Use code formatting for filename
        elif isinstance(value, bool):
            display_value = "Enabled" if value else "Disabled" # Display booleans nicely

        # Add field to embed
        embed.add_field(name=key.replace('_', ' ').title(), value=display_value, inline=False)

    await ctx.send(embed=embed)

@configcmd.error
async def config_error(ctx, error):
    """Error handler for the config command group."""
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("❌ Configuration commands can only be used within a server.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permissions to use configuration commands.")
    elif isinstance(error, commands.CommandInvokeError):
        # Show the original error that occurred within the command
        await ctx.send(f"An error occurred while executing the command: {error.original}")
        traceback.print_exc() # Log the full traceback for debugging
    else:
        # Catch any other unexpected errors
        await ctx.send(f"An unexpected error occurred: {error}")

# Decorators must be on separate lines
@configcmd.command(name='set')
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_set(ctx, setting: str, *, value: str):
    """Sets a config value. Usage: !config set <setting> <value>"""
    guild_id = ctx.guild.id
    # Get a copy of the current config to modify
    guild_config = get_guild_config(guild_id).copy()
    # Normalize setting name (lowercase, underscores)
    setting = setting.lower().replace('-', '_')

    # Define valid settings that can be changed via command
    valid_settings = [
        'similarity_threshold', 'hash_size', 'react_to_duplicates',
        'delete_duplicates', 'duplicate_reaction_emoji',
        'duplicate_scope', 'duplicate_check_mode'
    ]
    # Check if the provided setting is valid
    if setting not in valid_settings:
        await ctx.send(f"❌ Unknown setting '{setting}'. Settable keys: `{', '.join(valid_settings)}`")
        return

    original_value = guild_config.get(setting) # Get original value for feedback message
    new_value = None # Variable to store the validated new value
    error_msg = None # Variable to store validation error messages

    # Validate and coerce the input value based on the setting name
    try:
        if setting == 'duplicate_scope':
            value_lower = value.lower()
            if value_lower in VALID_SCOPES: new_value = value_lower
            else: error_msg = f"Invalid scope. Use: `{', '.join(VALID_SCOPES)}`"
        elif setting == 'duplicate_check_mode':
             value_lower = value.lower()
             if value_lower in VALID_CHECK_MODES: new_value = value_lower
             else: error_msg = f"Invalid mode. Use: `{', '.join(VALID_CHECK_MODES)}`"
        elif setting == 'similarity_threshold':
            new_value = int(value)
            if new_value < 0: error_msg = "Threshold must be 0 or greater."
        elif setting == 'hash_size':
            new_value = int(value)
            # Practical minimum hash size for imagehash
            if new_value < 4: error_msg = "Hash size must be at least 4."
        elif setting in ['react_to_duplicates', 'delete_duplicates']:
            # Flexible boolean parsing
            if value.lower() in ['true', 'on', 'yes', '1', 'enable', 'enabled']: new_value = True
            elif value.lower() in ['false', 'off', 'no', '0', 'disable', 'disabled']: new_value = False
            else: error_msg = "Value must be true/false (or on/off, yes/no, 1/0)."
        elif setting == 'duplicate_reaction_emoji':
            # Validate emoji by trying to react with it
            try:
                await ctx.message.add_reaction(value)
                # Clean up the test reaction immediately
                await ctx.message.remove_reaction(value, ctx.me)
                new_value = value # Store the valid emoji
            except discord.HTTPException:
                error_msg = "Invalid emoji provided. Please use a standard Unicode emoji or a custom emoji the bot can access."
    except ValueError:
        error_msg = f"Invalid value format for '{setting}'. Expected a number."
    except Exception as e:
        error_msg = f"Unexpected validation error: {e}"

    # If validation failed, send error message and stop
    if error_msg:
        await ctx.send(f"❌ Error setting '{setting}': {error_msg}")
        return

    # Check if a valid new value was determined (or if setting a boolean to False)
    is_boolean_setting = setting in ['react_to_duplicates', 'delete_duplicates']
    if new_value is not None or (is_boolean_setting and new_value is False) :
        # Update the configuration dictionary (copy)
        guild_config[setting] = new_value
        # Save the updated configuration for this guild
        if await save_guild_config(guild_id, guild_config):
            await ctx.send(f"✅ Updated '{setting}' for this server from `{original_value}` to `{new_value}`.")
            # Add warning if changing scope with existing data
            if setting == 'duplicate_scope':
                 await ctx.send(f"⚠️ **Warning:** Changing scope might affect how existing hashes are read/stored. If you have existing data in `{guild_config['hash_db_file']}`, consider clearing it manually if switching between `server` and `channel` scopes.")
        else:
            # Inform user if saving failed
            await ctx.send(f"⚠️ Failed to save configuration update to file.")
    else:
        # Should not happen if validation logic is correct, but as a fallback
         await ctx.send(f"❌ Could not determine a valid value for '{setting}' from input '{value}'.")


# Decorators must be on separate lines
@configcmd.group(name='channel', invoke_without_command=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel(ctx):
    """Manage the allowed channel list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id)
    channel_list = guild_config.get('allowed_channel_ids')
    if channel_list:
        embed = discord.Embed(title=f"Allowed Channels for {ctx.guild.name}", description='\n'.join(f"- <#{ch_id}> (`{ch_id}`)" for ch_id in channel_list), color=discord.Color.blue())
        await ctx.send(embed=embed)
    else: await ctx.send("ℹ️ Monitoring all channels in this server.")

# Decorators must be on separate lines
@config_channel.command(name='add')
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_add(ctx, channel: discord.TextChannel):
    """Adds a channel to the allowed list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    # Initialize list if it's currently None
    if guild_config.get('allowed_channel_ids') is None: guild_config['allowed_channel_ids'] = []
    # Add channel if not already present
    if channel_id not in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].append(channel_id)
        # Save the updated config
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Added <#{channel_id}> to allowed list.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> already allowed.")

# Decorators must be on separate lines
@config_channel.command(name='remove')
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_remove(ctx, channel: discord.TextChannel):
    """Removes a channel from the allowed list for this server."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy(); channel_id = channel.id
    # Check if the list exists and the channel is in it
    if guild_config.get('allowed_channel_ids') and channel_id in guild_config['allowed_channel_ids']:
        guild_config['allowed_channel_ids'].remove(channel_id)
        # If list becomes empty after removal, set it back to None
        if not guild_config['allowed_channel_ids']: guild_config['allowed_channel_ids'] = None
        # Save the updated config
        if await save_guild_config(guild_id, guild_config): await ctx.send(f"✅ Removed <#{channel_id}> from allowed list.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send(f"ℹ️ <#{channel_id}> not in allowed list.")

# Decorators must be on separate lines
@config_channel.command(name='clear')
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def config_channel_clear(ctx):
    """Clears the allowed channel list for this server (monitors all)."""
    guild_id = ctx.guild.id; guild_config = get_guild_config(guild_id).copy()
    # Check if the list is already None or empty
    if guild_config.get('allowed_channel_ids') is not None:
        guild_config['allowed_channel_ids'] = None # Set to None to monitor all
        # Save the updated config
        if await save_guild_config(guild_id, guild_config): await ctx.send("✅ Cleared allowed channels. Monitoring all.")
        else: await ctx.send(f"⚠️ Failed to save config.")
    else: await ctx.send("ℹ️ Already monitoring all channels.")

# --- Main Execution ---
if __name__ == "__main__":
    # Check for bot token before starting
    if BOT_TOKEN is None:
        print("Error: DISCORD_BOT_TOKEN not found in .env file or environment variables.", file=sys.stderr)
        sys.exit(1)

    try:
        print("Starting bot...")
        # Run the bot with the token
        bot.run(BOT_TOKEN)
    except discord.LoginFailure:
        print("Error: Invalid Discord Bot Token. Please check the token in your .env file.", file=sys.stderr)
        traceback.print_exc()
    except discord.PrivilegedIntentsRequired:
         # Specific error for missing intents
         print("Error: Privileged Intents (Message Content) are not enabled for the bot.", file=sys.stderr)
         print("Please go to your bot's settings in the Discord Developer Portal and enable the 'MESSAGE CONTENT INTENT'.", file=sys.stderr)
         traceback.print_exc()
    except Exception as e:
        # Catch any other exceptions during startup or runtime
        print(f"An error occurred while running the bot: {e}", file=sys.stderr)
        traceback.print_exc()
    finally:
        # This block executes whether the bot stops normally or due to an error
        print("DEBUG: Bot run loop finished or encountered an error.")


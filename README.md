# Discord Duplicate Image Detection Bot

## Overview

This Discord bot helps server administrators detect and manage duplicate or near-duplicate images posted by users. It's particularly useful for scenarios like verifying unique submissions (e.g., proof screenshots for contests or tasks).

The bot uses perceptual hashing (specifically, dhash) to compare images based on their visual content rather than exact pixel data. This allows it to identify images that are very similar, even if they have minor differences due to cropping, compression, or slight edits.

## Key Features

* **Perceptual Hashing:** Detects visually similar images, not just exact duplicates.
* **Per-Server Configuration:** All settings (thresholds, actions, scope, etc.) are managed independently for each server the bot is in.
* **Configurable Duplicate Scope:** Choose whether to check for duplicates across the entire server (`server` scope) or only within the specific channel where an image is posted (`channel` scope).
* **Configurable Check Mode:**
    * `strict`: Any similar image is flagged as a duplicate.
    * `owner_allowed`: Only flags duplicates if the user posting now is *different* from the user who posted the original image.
* **Configurable Actions:** Choose to have the bot:
    * Reply to the user posting a duplicate.
    * React to the duplicate message with an emoji.
    * Delete the duplicate message (requires "Manage Messages" permission).
* **Discord Command Management:** Server administrators can view and modify bot settings directly within Discord using commands (default prefix `!`).
* **Secure Token Handling:** Uses a `.env` file to keep the bot token private.

## Requirements

* **Python:** Version 3.11 or 3.12 recommended (due to potential compatibility issues with dependencies in 3.13+).
* **Required Libraries:**
    * `discord.py` (v2.x recommended)
    * `Pillow`
    * `ImageHash`
    * `python-dotenv`

## Installation and Setup

1.  **Get the Code:** Download or clone the Python script (`discord_duplicate_bot_vX.py`).
2.  **Install Libraries:** Open your terminal or command prompt in the script's directory and install the required libraries:
    ```bash
    pip install discord.py Pillow ImageHash python-dotenv
    ```
3.  **Create Discord Bot Application:**
    * Go to the [Discord Developer Portal](https://discord.com/developers/applications).
    * Click "New Application". Give it a name (e.g., "Duplicate Checker").
    * Navigate to the "Bot" tab. Click "Add Bot".
    * **Enable Privileged Gateway Intents:** Scroll down and ensure the following intents are **enabled** (toggled on/blue):
        * `PRESENCE INTENT` (Optional but recommended)
        * `SERVER MEMBERS INTENT` (Optional but recommended)
        * **`MESSAGE CONTENT INTENT` (Required!)**
    * **Copy Bot Token:** Under the bot's username, click "Reset Token" (or "View Token") and copy the token. **Keep this secret!**
4.  **Create `.env` File:** In the same directory as the Python script, create a file named `.env` and add your token:
    ```dotenv
    DISCORD_BOT_TOKEN=YOUR_ACTUAL_BOT_TOKEN_HERE
    ```
    *(Replace `YOUR_ACTUAL_BOT_TOKEN_HERE` with the token you copied)*
5.  **Invite Bot to Server:**
    * In the Developer Portal, go to "OAuth2" -> "URL Generator".
    * In "Scopes", check the `bot` box.
    * In "Bot Permissions" below, check the necessary permissions:
        * `View Channels`
        * `Send Messages`
        * `Read Message History`
        * `Add Reactions` (if `react_to_duplicates` is True)
        * `Manage Messages` (if `delete_duplicates` is True)
    * Copy the generated URL at the bottom.
    * Paste the URL into your browser and follow the prompts to add the bot to your desired server(s).

## Configuration Files

The bot uses several files (created automatically if they don't exist) in the same directory as the script:

1.  **`.env`:** (Created manually) Stores the `DISCORD_BOT_TOKEN`.
2.  **`server_configs.json`:** Stores configuration settings for *all* servers the bot is in, keyed by server ID. You can manage these settings via Discord commands.
    * `hash_db_file`: Name of the file storing hashes for this server (auto-generated).
    * `hash_size`: Detail level for hashing (default: 8).
    * `similarity_threshold`: Max difference allowed for duplicates (default: 5, lower is stricter).
    * `allowed_channel_ids`: List of channel IDs to monitor, or `null` for all (default: null).
    * `react_to_duplicates`: `true`/`false` to react to duplicates (default: true).
    * `delete_duplicates`: `true`/`false` to delete duplicates (default: false).
    * `duplicate_reaction_emoji`: Emoji used for reactions (default: "⚠️").
    * `duplicate_scope`: Where to check for duplicates (`"server"` or `"channel"`, default: "server").
    * `duplicate_check_mode`: How to handle duplicates (`"strict"` or `"owner_allowed"`, default: "strict").
3.  **`hashes_<guild_id>.json`:** Stores the actual image hashes for a specific server. The internal structure depends on the `duplicate_scope` setting. It now includes the `user_id` of the original poster.

## Running the Bot

1.  Open your terminal or command prompt.
2.  Navigate to the directory containing the Python script and the `.env` file.
3.  Run the script:
    ```bash
    python your_script_name.py
    ```
    *(Replace `your_script_name.py` with the actual filename)*

The bot should log in, print startup messages to the console, and appear online in the servers it has been invited to.

## Discord Commands (Admin Only)

These commands allow server administrators to manage the bot's settings for their specific server. (Default prefix: `!`)

* **`!config`**
    * Shows the current configuration settings for the server where the command is used.

* **`!config set <setting_name> <value>`**
    * Changes a specific setting.
    * **Available Settings:**
        * `similarity_threshold <number>` (e.g., `!config set similarity_threshold 3`)
        * `hash_size <number>` (e.g., `!config set hash_size 16`)
        * `react_to_duplicates <true|false>` (e.g., `!config set react_to_duplicates false`)
        * `delete_duplicates <true|false>` (e.g., `!config set delete_duplicates true`)
        * `duplicate_reaction_emoji <emoji>` (e.g., `!config set duplicate_reaction_emoji ❌`)
        * `duplicate_scope <server|channel>` (e.g., `!config set duplicate_scope channel`)
        * `duplicate_check_mode <strict|owner_allowed>` (e.g., `!config set duplicate_check_mode owner_allowed`)

* **`!config channel`**
    * Shows the list of channels currently being monitored (if specific channels are set).

* **`!config channel add <#channel_mention>`**
    * Adds a channel to the list of monitored channels.
    * Example: `!config channel add #image-proofs`

* **`!config channel remove <#channel_mention>`**
    * Removes a channel from the monitored list.
    * Example: `!config channel remove #general`

* **`!config channel clear`**
    * Clears the specific channel list, making the bot monitor all channels in the server it can see.

## Important Notes

* **Hash File Format:** The latest version stores hashes along with the original `user_id`. Hash files created by older versions will not have this ID. If you switch to `owner_allowed` mode with old hash files, the bot won't know the original owner and will likely treat all matches as violations. It's recommended to start with fresh hash files when enabling `owner_allowed` mode or if migrating from a much older version.
* **Changing Scope:** If you change the `duplicate_scope` between `server` and `channel` for a server that already has saved hashes, the bot might not be able to correctly read the old hashes due to the structural difference in the JSON file. Consider clearing the relevant `hashes_<guild_id>.json` file if you change the scope significantly.
* **Permissions:** Ensure the bot has the necessary permissions in the channels it needs to monitor (View Channel, Send Messages, Read Message History, Add Reactions, Manage Messages if deleting).

## Troubleshooting

* **Bot Offline:** Check if the script is running in the terminal. Ensure the `.env` file has the correct `DISCORD_BOT_TOKEN`.
* **No Response/Errors on Startup:** Verify the **Message Content Intent** is enabled in the Developer Portal. Check for any error messages in the console.
* **Bot Not Seeing Images:** Ensure the bot is invited to the server and has "View Channel" and "Read Message History" permissions in the relevant channel. Check the `allowed_channel_ids` setting using `!config`.
* **Commands Not Working:** Ensure you are using the correct prefix (`!`) and have Administrator permissions in the server.

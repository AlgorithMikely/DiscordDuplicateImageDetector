# Discord Duplicate Image Detection Bot V2

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
* **Configurable Time Limit:** Set a duration (in days) for how long past images should be considered for duplicate checks (e.g., only check against images from the last 30 days).
* **Configurable Actions:** Choose to have the bot:
    * Reply to the user posting a duplicate.
    * React to the duplicate message with an emoji.
    * Delete the duplicate message (requires "Manage Messages" permission).
* **User Allowlisting:** Exempt specific users from duplicate checks.
* **History Scanning:** Manually scan a channel's history to populate the hash database.
* **Hash Management:** Manually remove specific image hashes or clear the database.
* **Discord Command Management:** Server administrators can view and modify bot settings directly within Discord using commands (default prefix `!`).
* **Secure Token Handling:** Uses a `.env` file to keep the bot token private.

## Requirements

* **Python:** Version 3.11 or 3.12 recommended (due to potential compatibility issues with dependencies in 3.13+).
* **Required Libraries:** Listed in `requirements.txt`. Install using: `pip install -r requirements.txt`
    * `discord.py>=2.0.0`
    * `Pillow>=9.0.0`
    * `ImageHash>=4.2.0`
    * `python-dotenv>=0.19.0`
    * `python-dateutil` (for timestamp parsing)

## Installation and Setup

1.  **Get the Code:** Download or clone the Python script (e.g., `discord_duplicate_bot.py`) and the `requirements.txt` file.
2.  **Install Libraries:** Open your terminal or command prompt in the script's directory and run:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Create Discord Bot Application:**
    * Go to the [Discord Developer Portal](https://discord.com/developers/applications).
    * Click "New Application". Give it a name.
    * Navigate to the "Bot" tab. Click "Add Bot".
    * **Enable Privileged Gateway Intents:** Ensure these are **enabled** (toggled on/blue):
        * `PRESENCE INTENT` (Optional)
        * `SERVER MEMBERS INTENT` (Optional)
        * **`MESSAGE CONTENT INTENT` (Required!)**
    * **Copy Bot Token:** Click "Reset Token" (or "View Token") and copy the token. **Keep it secret!**
4.  **Create `.env` File:** In the same directory as the Python script, create a file named `.env` and add your token:
    ```dotenv
    DISCORD_BOT_TOKEN=YOUR_ACTUAL_BOT_TOKEN_HERE
    ```
    *(Replace `YOUR_ACTUAL_BOT_TOKEN_HERE` with your token)*
5.  **Invite Bot to Server:**
    * In the Developer Portal, go to "OAuth2" -> "URL Generator".
    * In "Scopes", check `bot`.
    * In "Bot Permissions", check:
        * `View Channels`
        * `Send Messages`
        * `Read Message History`
        * `Add Reactions` (if `react_to_duplicates` is True)
        * `Manage Messages` (if `delete_duplicates` is True)
    * Copy the generated URL.
    * Paste the URL into your browser and add the bot to your server(s).

## Configuration Files

The bot uses several files (created automatically if they don't exist) in the same directory as the script:

1.  **`.env`:** (Created manually) Stores the `DISCORD_BOT_TOKEN`.
2.  **`server_configs.json`:** Stores configuration settings for *all* servers, keyed by server ID. Manage via Discord commands.
    * `hash_db_file`: Name of the hash file for this server (auto-generated).
    * `hash_size`: Detail level for hashing (default: 8).
    * `similarity_threshold`: Max difference for duplicates (default: 5).
    * `allowed_channel_ids`: List of channel IDs to monitor, or `null` for all (default: null).
    * `react_to_duplicates`: `true`/`false` (default: true).
    * `delete_duplicates`: `true`/`false` (default: false).
    * `duplicate_reaction_emoji`: Emoji for reactions (default: "⚠️").
    * `duplicate_scope`: `"server"` or `"channel"` (default: "server").
    * `duplicate_check_mode`: `"strict"` or `"owner_allowed"` (default: "strict").
    * `duplicate_check_duration_days`: How many days back to check (default: 0, meaning forever).
    * `allowed_users`: List of user IDs exempt from checks (default: []).
3.  **`hashes_<guild_id>.json`:** Stores image hashes, original user IDs, and timestamps for a specific server. Structure depends on `duplicate_scope`.

## Running the Bot

1.  Open your terminal or command prompt.
2.  Navigate to the directory containing the Python script, `.env`, and `requirements.txt`.
3.  Run the script:
    ```bash
    python your_script_name.py
    ```
    *(Replace `your_script_name.py` with the actual filename)*

The bot should log in, print startup messages, and appear online.

## Discord Commands (Admin Only)

These commands manage the bot's settings and data for the server where they are used. (Default prefix: `!`)

* **`!help`**: Shows a list of available commands and their usage.

* **`!config`**: Shows the current configuration settings for this server.

* **`!config set <setting_name> <value>`**: Changes a specific setting.
    * **Available Settings:**
        * `similarity_threshold <number>` (e.g., `3`)
        * `hash_size <number>` (e.g., `16`)
        * `react_to_duplicates <true|false>`
        * `delete_duplicates <true|false>`
        * `duplicate_reaction_emoji <emoji>` (e.g., `❌`)
        * `duplicate_scope <server|channel>`
        * `duplicate_check_mode <strict|owner_allowed>`
        * `duplicate_check_duration_days <days>` (e.g., `30`; use `0` for forever)

* **`!config channel`**: Shows the list of channels currently monitored.
* **`!config channel add <#channel_mention>`**: Adds a channel to the monitored list.
* **`!config channel remove <#channel_mention>`**: Removes a channel from the monitored list.
* **`!config channel clear`**: Clears the list (monitors all channels).

* **`!allowlist`**: Shows the list of users exempt from duplicate checks.
* **`!allowlist add <user_mention_or_id>`**: Adds a user to the exemption list.
* **`!allowlist remove <user_mention_or_id>`**: Removes a user from the exemption list.

* **`!removehash <message_link_or_id>`**: Removes the stored hash associated with a specific message. Useful for correcting errors.
* **`!clearhashes [channel_mention] --confirm`**: Clears *all* stored hashes for the entire server, or just for the specified channel if provided (only works if scope is 'channel'). **Requires `--confirm` flag.** Use with caution!

* **`!scan <#channel_mention> [limit]`**: Scans message history in a channel (up to `limit` messages, default 1000) and adds any unique images found to the hash database. Does not flag old messages.

## Important Notes

* **Hash File Format:** The bot now stores hashes with `user_id` and `timestamp`. Older hash files lacking these fields might cause `owner_allowed` or time-limit checks to behave unexpectedly for those entries. It's best if these features are used with data generated by this version or newer.
* **Changing Scope:** Switching `duplicate_scope` between `server` and `channel` on a server with existing hashes can lead to mismatches in how data is stored and read. Consider clearing hashes (`!clearhashes --confirm`) before changing scope if necessary.
* **History Scan:** The `!scan` command can be resource-intensive and may take time on channels with large histories or many images. Avoid running frequent or overlapping scans.
* **Permissions:** Ensure the bot has necessary permissions (View Channel, Send Messages, Read Message History, Add Reactions, Manage Messages if deleting).

## Troubleshooting

* **Bot Offline:** Check terminal for errors. Verify `.env` token.
* **No Response/Errors on Startup:** Ensure **Message Content Intent** is enabled in Developer Portal. Check console output.
* **Bot Not Seeing Images:** Verify bot permissions in the channel. Check `!config` for `allowed_channel_ids`.
* **Commands Not Working:** Check prefix (`!`). Ensure user has Administrator permissions. Check console for errors.

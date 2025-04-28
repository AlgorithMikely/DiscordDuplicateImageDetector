# Discord Duplicate Image Detection Bot (Slash Commands)

## Overview

This Discord bot helps server administrators detect and manage duplicate or near-duplicate images posted by users. It's particularly useful for scenarios like verifying unique submissions (e.g., proof screenshots for contests or tasks).

The bot uses perceptual hashing (specifically, dhash) to compare images based on their visual content rather than exact pixel data. This allows it to identify images that are very similar, even if they have minor differences due to cropping, compression, or slight edits. This version uses **top-level Slash Commands** (`/`) for configuration and management.

## Key Features

* **Perceptual Hashing:** Detects visually similar images.
* **Slash Commands:** Modern Discord integration for easy command discovery and usage. All commands are top-level (e.g., `/config_view`, `/allowlist_add`).
* **Per-Server Configuration:** All settings are managed independently for each server.
* **Configurable Duplicate Scope:** Check duplicates server-wide (`server`) or per-channel (`channel`).
* **Configurable Check Mode:**
    * `strict`: Any similar image is flagged.
    * `owner_allowed`: Only flags if the poster is different from the original uploader.
* **Configurable Time Limit:** Set a duration (in days) for how long past images should be considered for duplicate checks (0 for forever).
* **Configurable Actions:** Choose to have the bot:
    * Reply to the user posting a duplicate (with a customizable message template).
    * React to the duplicate message with an emoji.
    * Delete the duplicate message (requires "Manage Messages" permission).
* **User Allowlisting:** Exempt specific users from duplicate checks.
* **History Scanning:** Manually scan a channel's history to populate the hash database and optionally flag/reply/delete non-oldest duplicates.
* **Hash Management:** Manually remove specific image hashes or clear the database.
* **Flag Clearing:** Manually remove the bot's warning reactions from messages in a channel.
* **Secure Token Handling:** Uses a `.env` file for the bot token.

## Requirements

* **Python:** Version 3.11 or 3.12 recommended (due to potential dependency compatibility issues in 3.13+).
* **Required Libraries:** Listed in `requirements.txt`. Install using: `pip install -r requirements.txt`
    * `discord.py>=2.0.0` (Pycord)
    * `Pillow>=9.0.0`
    * `ImageHash>=4.2.0`
    * `python-dotenv>=0.19.0`
    * `python-dateutil>=2.8.0`

## Installation and Setup

1.  **Get the Code:** Download or clone the Python script (e.g., `discord_checker_bot.py`) and the `requirements.txt` file.
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
        * `SERVER MEMBERS INTENT` (Optional but recommended)
        * **`MESSAGE CONTENT INTENT` (Required for reading image attachments!)**
    * **Copy Bot Token:** Click "Reset Token" (or "View Token") and copy the token. **Keep this secret!**
4.  **Create `.env` File:** In the same directory as the Python script, create a file named `.env` and add your token:
    ```dotenv
    DISCORD_BOT_TOKEN=YOUR_ACTUAL_BOT_TOKEN_HERE
    ```
    *(Replace `YOUR_ACTUAL_BOT_TOKEN_HERE` with your token)*
5.  **Invite Bot to Server:**
    * In the Developer Portal, go to "OAuth2" -> "URL Generator".
    * In "Scopes", check **`bot`** AND **`applications.commands`**. The `applications.commands` scope is necessary for slash commands to work.
    * In "Bot Permissions" below, check the necessary permissions:
        * `View Channels`
        * `Send Messages`
        * `Read Message History`
        * `Add Reactions` (if `react_to_duplicates` is True or using scan/clearflags)
        * `Manage Messages` (if `delete_duplicates` is True, either for new messages or during scan)
    * Copy the generated URL at the bottom.
    * Paste the URL into your browser and add the bot to your desired server(s).

## Configuration Files

The bot uses several files (created automatically if they don't exist) in the same directory as the script:

1.  **`.env`:** (Created manually) Stores the `DISCORD_BOT_TOKEN`.
2.  **`server_configs.json`:** Stores configuration settings for *all* servers, keyed by server ID. Manage via Discord slash commands.
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
    * `duplicate_reply_template`: Customizable string for reply messages. Default:
      ```
      {emoji} Hold on, {mention}! Image `{filename}` similar to recent submission (ID: `{identifier}`, Dist: {distance}{original_user_info}).{jump_link}
      ```
      *(See command description below for placeholder details)*
3.  **`hashes_<guild_id>.json`:** Stores image hashes, original user IDs, and timestamps for a specific server. Structure depends on `duplicate_scope`.

## Running the Bot

1.  Open your terminal or command prompt.
2.  Navigate to the directory containing the Python script, `.env`, and `requirements.txt`.
3.  Run the script:
    ```bash
    python your_script_name.py
    ```
    *(Replace `your_script_name.py` with the actual filename)*

The bot should log in, print startup messages (including syncing slash commands), and appear online. Slash commands might take a short while (up to an hour for global commands, usually faster for guild commands) to appear in Discord the first time.

## Discord Slash Commands (Admin Only)

These commands allow server administrators to manage the bot's settings and data for their specific server. Start typing `/` in Discord to see available commands.

**Configuration Management:**

* **`/config_view`**: Shows the current configuration settings for this server. (Response is ephemeral).
* **`/config_set_threshold value:<number>`**: Sets the similarity threshold (0-20, lower is stricter).
* **`/config_set_hash_size value:<number>`**: Sets the hash detail level (e.g., 8 or 16, min 4).
* **`/config_set_react value:<True|False>`**: Enable/disable reacting to duplicate messages.
* **`/config_set_delete value:<True|False>`**: Enable/disable deleting duplicate messages.
* **`/config_set_emoji value:<emoji>`**: Sets the emoji used for duplicate reactions.
* **`/config_set_scope value:<server|channel>`**: Sets duplicate check scope.
* **`/config_set_check_mode value:<strict|owner_allowed>`**: Sets duplicate check mode.
* **`/config_set_duration value:<days>`**: Sets how many days back to check for duplicates (0 = forever).
* **`/config_set_reply_template template:<string>`**: Sets the template for duplicate reply messages.
    * **Available Placeholders:**
        * `{mention}`: Mention of the user who posted the duplicate.
        * `{filename}`: Original filename of the duplicate image.
        * `{identifier}`: The internal identifier for the matched original image (message_id-filename).
        * `{distance}`: The hash distance (similarity score) between the images.
        * `{original_user_mention}`: Mention of the user who posted the original image (if known, otherwise "*Unknown*").
        * `{emoji}`: The currently configured `duplicate_reaction_emoji`.
        * `{original_user_info}`: Expands to ", Orig User: <@user_id>" if the original user is known, otherwise empty string.
        * `{jump_link}`: Expands to "\nOriginal: <message_link>" if the original message ID is known, otherwise empty string.

**Allowed Channel Management:**

* **`/config_channel_view`**: Shows the list of channels currently monitored.
* **`/config_channel_add channel:<#channel_mention>`**: Adds a channel to the monitored list.
* **`/config_channel_remove channel:<#channel_mention>`**: Removes a channel from the monitored list.
* **`/config_channel_clear`**: Clears the list (monitors all channels).

**User Allowlist Management:**

* **`/allowlist_view`**: Shows the list of users exempt from duplicate checks.
* **`/allowlist_add user:<user_mention_or_id>`**: Adds a user to the exemption list.
* **`/allowlist_remove user:<user_mention_or_id>`**: Removes a user from the exemption list.

**Hash Database Management:**

* **`/hash_remove message_reference:<message_link_or_id>`**: Removes the stored hash associated with a specific message.
* **`/hash_clear confirm:<True|False> [channel:<#channel_mention>]`**: Clears *all* stored hashes for the server, or just for the specified channel (if scope is 'channel'). **Requires `confirm:True`**.

**History Processing:**

* **`/scan channel:<#channel_mention> [limit:<number>] [flag_duplicates:<True|False>] [reply_to_duplicates:<True|False>] [delete_duplicates:<True|False>]`**: Scans message history, adds/updates hashes (prioritizing oldest), and optionally applies actions to non-oldest duplicates found.
* **`/clearflags channel:<#channel_mention> confirm:<True|False> [limit:<number>]`**: Removes the bot's warning reactions from messages in a channel's history. **Requires `confirm:True`**.

## Important Notes

* **Slash Command Scope:** Ensure the bot was invited with the `applications.commands` scope checked in addition to the `bot` scope.
* **Command Syncing:** Slash commands might take time to appear in Discord after the bot starts or joins a new server (up to an hour globally).
* **Hash File Format:** The bot stores hashes with `user_id` and `timestamp`. Older hash files lacking these fields might cause `owner_allowed` or time-limit checks to behave unexpectedly. Best used with data generated by this version or newer.
* **Changing Scope:** Switching `duplicate_scope` between `server` and `channel` on a server with existing hashes can lead to mismatches. Consider clearing hashes (`/hash_clear confirm:True`) before changing scope if necessary.
* **History Scan/Clear:** The `/scan` and `/clearflags` commands can be resource-intensive. Use with appropriate limits and be aware of potential rate limits on large channels. The action options on `/scan` can be noisy.
* **Permissions:** Ensure the bot has necessary permissions (View Channel, Send Messages, Read Message History, Add Reactions, Manage Messages if deleting). Admins need Administrator permission to use management commands.

## Troubleshooting

* **Bot Offline:** Check terminal for errors. Verify `.env` token.
* **No Response/Errors on Startup:** Verify **Message Content Intent** is enabled. Check console output.
* **Slash Commands Not Appearing:** Ensure bot was invited with `applications.commands` scope. Wait up to an hour. Check console for syncing errors. Restart the bot/Discord client.
* **Bot Not Seeing Images:** Verify bot permissions. Check `/config_view` for `allowed_channel_ids`. Check `/allowlist_view`.
* **Commands Not Working:** Ensure user has Administrator permissions. Check console for errors.

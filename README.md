# Discord Duplicate Image Detection Bot V4

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
* **History Scanning (`/scan`):** Manually scan a channel's history to populate the hash database and optionally flag/reply/delete/log non-oldest duplicates.
* **Hash Management:** Manually remove specific image hashes or clear the database.
* **Flag Clearing (`/clearflags`):** Manually remove the bot's warning reactions from messages in a channel.
* **Configurable Logging Channel:** Designate a specific channel for duplicate detection logs (for new messages and optionally for `/scan` results).
* **Startup Catch-up:** Optionally processes messages missed while the bot was offline to update the hash database (configurable limit per channel).
* **Robust Handling:** Includes fallbacks for reporting the status of long-running tasks (`/scan`, `/clearflags`) that exceed Discord's 15-minute interaction window.
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

1.  **Get the Code:** Download or clone the Python script (e.g., `discord_duplicate_bot_v4.py`) and the `requirements.txt` file.
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
        * `SERVER MEMBERS INTENT` (Optional but recommended for user lookups)
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
        * `Send Messages` (Required in channels it monitors/replies in, and the log channel if used)
        * `Embed Links` (Required for logging if log channel is used)
        * `Read Message History` (Required for `/scan`, `/clearflags`, and Startup Catch-up)
        * `Add Reactions` (Required if `react_to_duplicates` is True or using scan/clearflags)
        * `Manage Messages` (Required if `delete_duplicates` is True, either for new messages or during scan)
    * Copy the generated URL at the bottom.
    * Paste the URL into your browser and add the bot to your desired server(s).

## Configuration Files

The bot uses several files (created automatically if they don't exist) in the same directory as the script:

1.  **`.env`:** (Created manually) Stores the `DISCORD_BOT_TOKEN`.
2.  **`server_configs.json`:** Stores configuration settings for *all* servers, keyed by server ID. Manage via Discord slash commands.
    * `hash_db_file`: Name of the hash file for this server (auto-generated).
    * `hash_size`: Detail level for hashing (default: 8).
    * `similarity_threshold`: Max difference for duplicates (default: 5).
    * `allowed_channel_ids`: List of channel IDs to monitor, or `null` for all (default: null). Affects both new messages and startup catch-up.
    * `react_to_duplicates`: `true`/`false` (default: true). For new duplicates.
    * `delete_duplicates`: `true`/`false` (default: false). For new duplicates.
    * `reply_on_duplicate`: `true`/`false` (default: true). Controls replies for newly posted duplicates.
    * `duplicate_reaction_emoji`: Emoji for reactions (default: "⚠️").
    * `duplicate_scope`: `"server"` or `"channel"` (default: "server").
    * `duplicate_check_mode`: `"strict"` or `"owner_allowed"` (default: "strict").
    * `duplicate_check_duration_days`: How many days back to check (default: 0, meaning forever).
    * `allowed_users`: List of user IDs exempt from checks (default: []).
    * `log_channel_id`: Channel ID for logging duplicate events, or `null` to disable (default: null).
    * `duplicate_reply_template`: Customizable string for reply messages. Default:
        ```
        {emoji} Hold on, {mention}! Image `{filename}` similar to recent submission (ID: `{identifier}`, Dist: {distance}{original_user_info}).{jump_link}
        ```
      *(See command description below for placeholder details)*
    * `enable_catchup_on_startup`: `true`/`false` (default: false). Enables checking missed messages on startup.
    * `catchup_limit_per_channel`: Max messages to check per channel during catch-up (default: 100).
3.  **`hashes_<guild_id>.json`:** Stores image hashes, original user IDs, and timestamps for a specific server. Structure depends on `duplicate_scope`.
4.  **`last_seen.json`:** Stores the timestamp of the bot's last successful startup (UTC). Used for the catch-up feature.

## Running the Bot

1.  Open your terminal or command prompt.
2.  Navigate to the directory containing the Python script, `.env`, and `requirements.txt`.
3.  Run the script:
    ```bash
    python discord_duplicate_bot_v4.py
    ```

The bot should log in, print startup messages (including syncing slash commands), potentially run the catch-up process if enabled, and appear online. Slash commands might take a short while (up to an hour for global commands, usually faster for guild commands) to appear in Discord the first time. Check the console for any startup errors (e.g., invalid token, missing intents).

## Discord Slash Commands (Admin Only)

These commands allow server administrators to manage the bot's settings and data for their specific server. Start typing `/` in Discord to see available commands.

**Configuration Management:**

* **`/config_view`**: Shows the current configuration settings for this server. (Response is ephemeral).
* **`/config_set_threshold value:<number>`**: Sets the similarity threshold (0-20, lower is stricter).
* **`/config_set_hash_size value:<number>`**: Sets the hash detail level (e.g., 8 or 16, min 4).
* **`/config_set_react value:<True|False>`**: Enable/disable reacting to **newly posted** duplicate messages.
* **`/config_set_delete value:<True|False>`**: Enable/disable deleting **newly posted** duplicate messages.
* **`/config_set_reply value:<True|False>`**: Enable/disable replying to **newly posted** duplicate messages.
* **`/config_set_emoji value:<emoji>`**: Sets the emoji used for duplicate reactions.
* **`/config_set_scope value:<server|channel>`**: Sets duplicate check scope.
* **`/config_set_check_mode value:<strict|owner_allowed>`**: Sets duplicate check mode.
* **`/config_set_duration value:<days>`**: Sets how many days back to check for duplicates (0 = forever).
* **`/config_set_log_channel [channel:<#channel_mention>]`**: Sets the channel for logging duplicate events. Omit channel to disable logging. Requires Send Messages/Embed Links permission in the target channel.
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
* **`/config_set_catchup_enabled value:<True|False>`**: Enable/disable checking missed messages on bot startup.
* **`/config_set_catchup_limit value:<number>`**: Sets max messages per channel (10-1000) to check during startup catch-up.

**Allowed Channel Management:**

* **`/config_channel_view`**: Shows the list of channels currently monitored (or "All Channels").
* **`/config_channel_add channel:<#channel_mention>`**: Adds a channel to the monitored list. If the list was previously empty, this restricts monitoring (for new posts and catch-up) to *only* the specified channel(s).
* **`/config_channel_remove channel:<#channel_mention>`**: Removes a channel from the monitored list. If the list becomes empty, monitoring reverts to *all* channels.
* **`/config_channel_clear`**: Clears the list, causing the bot to monitor *all* channels (for new posts and catch-up).

**User Allowlist Management:**

* **`/allowlist_view`**: Shows the list of users exempt from duplicate checks.
* **`/allowlist_add user:<user_mention_or_id>`**: Adds a user to the exemption list.
* **`/allowlist_remove user:<user_mention_or_id>`**: Removes a user from the exemption list.

**Hash Database Management:**

* **`/hash_remove message_reference:<message_link_or_id>`**: Removes the stored hash(es) associated with a specific message.
* **`/hash_clear confirm:<True|False> [channel:<#channel_mention>]`**: Clears *all* stored hashes for the server, or just for the specified channel (if scope is 'channel'). **Requires `confirm:True`**.

**History Processing:**

* **`/scan channel:<#channel_mention> [limit:<number>] [flag_duplicates:<True|False>] [reply_to_duplicates:<True|False>] [delete_duplicates:<True|False>] [log_scan_duplicates:<True|False>]`**:
    * Scans message history in the specified `channel` up to the `limit`.
    * Adds/updates hashes in the database (prioritizing the oldest found message for each unique hash).
    * Optionally applies actions (`flag`, `reply`, `delete`) to messages containing duplicates that are *not* the oldest one found.
    * Optionally (`log_scan_duplicates:True`) sends a detailed log message to the configured log channel for each non-oldest duplicate found during the scan.
    * Reports overall completion status by editing the initial status message, or sending a new message if the scan takes longer than 15 minutes.
* **`/clearflags channel:<#channel_mention> confirm:<True|False> [limit:<number>]`**:
    * Removes the bot's configured warning reactions (`duplicate_reaction_emoji`) from messages in the specified `channel`'s history up to the `limit`.
    * **Requires `confirm:True`**.
    * Reports completion status similarly to `/scan`, using a fallback message if the process exceeds 15 minutes.

## Important Notes

* **Slash Command Scope:** Ensure the bot was invited with the `applications.commands` scope checked in addition to the `bot` scope.
* **Command Syncing:** Slash commands might take time to appear in Discord after the bot starts or joins a new server (up to an hour globally). Restarting your Discord client can sometimes help.
* **Hash File Format:** The bot stores hashes with `user_id` and `timestamp`. Older hash files lacking these fields might cause `owner_allowed` or time-limit checks to behave unexpectedly. `/scan` will add missing timestamps where possible.
* **Changing Scope:** Switching `duplicate_scope` between `server` and `channel` on a server with existing hashes can lead to mismatches. Consider clearing hashes (`/hash_clear confirm:True`) before changing scope if necessary.
* **Startup Catch-up:**
    * If enabled (`/config_set_catchup_enabled True`), the bot checks messages posted since its last startup.
    * It checks channels listed in `allowed_channel_ids`. If this list is empty/not set, it checks *all* channels the bot can read history in.
    * Uses the `catchup_limit_per_channel` setting to limit how many messages are checked per channel.
    * Only adds/updates hashes in the database; it does **not** perform actions like replying or reacting during catch-up.
* **Long Running Tasks (`/scan`, `/clearflags`):**
    * These commands can be resource-intensive on channels with many messages or images. Use appropriate limits.
    * Discord interaction tokens expire after 15 minutes. If these commands take longer than that, the bot cannot edit the initial "Scanning..." or "Clearing..." status message.
    * **Workaround:** The bot will attempt to delete the original status message and then send a **new message** in the same channel with the final results if the 15-minute window is exceeded.
    * Using `log_scan_duplicates:True` on large scans can generate many log messages; use judiciously.
* **Permissions:** Ensure the bot has the necessary permissions listed in the "Installation and Setup" section. Admins need Administrator permission on the server to use management commands. The bot also needs specific permissions in the target channels for actions like reacting, deleting, replying, and reading history, as well as in the log channel if configured and used.
* **Error Logging:** Most operational information (startup, config loading) and detailed errors/tracebacks are printed to the **console/terminal** where the bot script is running. Check there for debugging information.

## Troubleshooting

* **Bot Offline:** Check terminal for errors (e.g., `LoginFailure`, `PrivilegedIntentsRequired`). Verify `.env` token is correct and present.
* **No Response/Errors on Startup:** Verify **Message Content Intent** is enabled in the Discord Developer Portal. Check console output for errors during startup. Check `last_seen.json` if catch-up seems stuck.
* **Slash Commands Not Appearing:** Ensure bot was invited with `applications.commands` scope. Wait up to an hour. Check console for syncing errors (`Failed to sync slash commands`). Restart the bot/Discord client. Try re-inviting the bot.
* **Bot Not Seeing Images/Detecting Duplicates:** Verify bot permissions in the specific channel/server settings. Check `/config_view` for `allowed_channel_ids`. Check `/allowlist_view` if the user posting is exempt. Check console for processing errors during the `on_message` event.
* **Commands Not Working:** Ensure user invoking the command has Administrator permission on the server. Check console for specific command errors or permission denials.
* **`/scan` or `/clearflags` finished but sent a new message instead of editing:** This is expected behavior if the task took longer than 15 minutes due to Discord limitations; the final status is still reported correctly in the new message. Check the console log for confirmation.
* **Permission Errors during `/scan` or `/clearflags` actions:** Ensure the bot has the required permissions (Add Reactions, Send Messages, Manage Messages, Send/Embed in Log Channel) in the channel being scanned/cleared *and* in the log channel if logging is enabled.
* **Catch-up Not Working:** Ensure `enable_catchup_on_startup` is `True` via `/config_view`. Ensure the bot has `Read Message History` permission in the channels it should be checking (either all channels or those specified in `allowed_channel_ids`). Check the console logs during startup for messages related to the catch-up process.


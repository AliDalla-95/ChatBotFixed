import re
from googleapiclient.discovery import build

def get_youtube_channel_name(api_key, url):
    """
    Returns YouTube channel name from a channel URL
    Requires: YouTube Data API v3 key
    """
    # Extract channel ID or custom URL from different URL formats
    patterns = [
        r"youtube\.com/channel/([^/]+)",         # Channel ID URL
        r"youtube\.com/c/([^/]+)",               # Custom URL
        r"youtube\.com/user/([^/]+)",            # Legacy user URL
        r"youtube\.com/@([^/]+)"                 # Handle URL
    ]

    channel_identifier = None
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            channel_identifier = match.group(1)
            break

    if not channel_identifier:
        return "Invalid YouTube channel URL"

    youtube = build('youtube', 'v3', developerKey=api_key)

    try:
        # Try direct channel ID lookup
        if url.startswith("https://www.youtube.com/channel/"):
            request = youtube.channels().list(
                part="snippet",
                id=channel_identifier
            )
            response = request.execute()
            if response['items']:
                return response['items'][0]['snippet']['title']

        # Handle other URL types (custom URL, user URL, handle)
        search_request = youtube.search().list(
            q=channel_identifier,
            part="snippet",
            type="channel",
            maxResults=1
        )
        search_response = search_request.execute()
        
        if search_response['items']:
            channel_id = search_response['items'][0]['snippet']['channelId']
            channel_request = youtube.channels().list(
                part="snippet",
                id=channel_id
            )
            channel_response = channel_request.execute()
            return channel_response['items'][0]['snippet']['title']

        return "Channel not found"

    except Exception as e:
        return f"Error: {str(e)}"

# Usage
API_KEY = "AIzaSyCH0lUUlI-u1ziHsHiSl8aTC2J0nFU2l2Q"  # Get from Google Cloud Console
CHANNEL_URL = "http://www.youtube.com/@IlhamAlMadfaiOfficial"

channel_name = get_youtube_channel_name(API_KEY, CHANNEL_URL)
print(f"Channel Name: {channel_name}")
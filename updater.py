import bpy
import urllib.request
import json
import zipfile
import os
import shutil

# Official Sparkle repository.
GITHUB_REPO = "michaelfroggy/Sparkle"

_REPO_PLACEHOLDER = "YOUR_USERNAME/Sparkle"

def get_latest_release_info():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={'User-Agent': 'Blender-Addon-Updater'})
    
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception as e:
        print(f"Failed to check for updates: {e}")
        return None

def parse_version_string(tag_name):
    # Removes 'v' and splits by '.'
    # e.g., "v1.2.3" -> (1, 2, 3)
    clean_tag = tag_name.replace("v", "").replace("V", "")
    parts = clean_tag.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0, 0, 0)

def check_for_update(current_version):
    """
    Returns (update_available, latest_version_tuple, download_url)
    """
    if GITHUB_REPO == _REPO_PLACEHOLDER:
        return False, current_version, "Please configure GITHUB_REPO in updater.py!"
        
    data = get_latest_release_info()
    if not data:
        return False, current_version, "Failed to connect to GitHub."
        
    latest_tag = data.get("tag_name", "")
    latest_version = parse_version_string(latest_tag)
    
    # Simple tuple comparison: (1, 0, 1) > (1, 0, 0)
    if latest_version > current_version:
        assets = data.get("assets", [])
        if assets:
            download_url = assets[0].get("browser_download_url")
            return True, latest_version, download_url
            
    return False, current_version, "You are on the latest version."

def install_update(download_url):
    """
    Downloads the ZIP and extracts it over the current addon folder.
    """
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(addon_dir, "update_temp.zip")
    
    req = urllib.request.Request(download_url, headers={'User-Agent': 'Blender-Addon-Updater'})
    
    try:
        # The timeout bounds each read, not the whole download: a stalled server gives up.
        with urllib.request.urlopen(req, timeout=60) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # The pack_zip script puts the files at the root of the ZIP
            zf.extractall(addon_dir)
            
        os.remove(zip_path)
        return True, "Update successful! Please restart Blender to apply changes."
    except Exception as e:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False, f"Update failed: {e}"

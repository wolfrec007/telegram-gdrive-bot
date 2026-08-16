import json
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import config


SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    if config.AUTH_MODE == "service_account":
        info = json.loads(config.SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = _load_oauth_creds()

    return build("drive", "v3", credentials=creds)


def _load_oauth_creds():
    try:
        with open("token.json") as f:
            return Credentials.from_authorized_user_file("token.json", SCOPES)
    except FileNotFoundError:
        raise RuntimeError("OAuth not authenticated yet. Visit /auth in your browser.")


def save_oauth_tokens(creds):
    with open("token.json", "w") as f:
        f.write(creds.to_json())


def get_oauth_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": config.OAUTH_CLIENT_ID,
                "client_secret": config.OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.OAUTH_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
    )


def upload_file(file_path, file_name, mime_type, folder_id=None):
    service = get_drive_service()
    body = {
        "name": file_name,
        "parents": [folder_id or config.GOOGLE_DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    file = service.files().create(body=body, media_body=media, fields="id, webViewLink").execute()
    return file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view")


def overwrite_file(file_id, file_path, mime_type):
    service = get_drive_service()
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    file = service.files().update(fileId=file_id, media_body=media, fields="id, webViewLink").execute()
    return file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view")


def find_file_by_name(name, folder_id):
    service = get_drive_service()
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
    result = service.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
    files = result.get("files", [])
    return files[0] if files else None


def list_folders(parent_id="root"):
    service = get_drive_service()
    query = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    result = service.files().list(q=query, fields="files(id, name)", orderBy="name", pageSize=50).execute()
    return result.get("files", [])


def get_folder_info(folder_id):
    service = get_drive_service()
    file = service.files().get(fileId=folder_id, fields="id, name, parents").execute()
    return file


def get_parent_id(folder_id):
    info = get_folder_info(folder_id)
    parents = info.get("parents", [])
    return parents[0] if parents else None


def create_folder(name, parent_id):
    service = get_drive_service()
    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    file = service.files().create(body=body, fields="id, name").execute()
    return file

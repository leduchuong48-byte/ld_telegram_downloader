"""web ui for media download — multi-account version"""

import asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_login import LoginManager, UserMixin, login_required, login_user

import utils
from module.app import Application
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    get_total_download_speed,
    set_download_state,
)
from utils.crypto import AesBase64
from utils.format import format_byte

if TYPE_CHECKING:
    from module.account_manager import AccountManager
    from module.web_auth import WebAuthManager

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_flask_app = Flask(__name__)

_flask_app.secret_key = "ld_tg_downloader"
_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
web_login_users: dict = {}
deAesCrypt = AesBase64("1234123412ABCDEF", "ABCDEF1234123412")

# ── multi-account globals (set by init_web_multi) ────────────────
_account_manager: Optional["AccountManager"] = None
_web_auth_manager: Optional["WebAuthManager"] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_start_account_callback = None
_stop_account_callback = None
_get_instance_callback = None  # (account_id) -> AccountInstance | None


class User(UserMixin):
    """Web Login User"""

    def __init__(self):
        self.sid = "root"

    @property
    def id(self):
        """ID"""
        return self.sid


@_login_manager.user_loader
def load_user(_):
    return User()


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def _run_async(coro):
    """Run an async coroutine from Flask (sync) context."""
    if _event_loop is None:
        raise RuntimeError("Event loop not set")
    future = asyncio.run_coroutine_threadsafe(coro, _event_loop)
    return future.result(timeout=120)


# ── legacy init (kept for backward compat) ───────────────────────


def run_web_server(app: Application):
    """Runs a web server using the Flask framework."""
    get_flask_app().run(
        app.web_host, app.web_port, debug=app.debug_web, use_reloader=False
    )


# pylint: disable = W0603
def init_web(app: Application):
    """Legacy single-account init."""
    global web_login_users
    if app.web_login_secret:
        web_login_users = {"root": app.web_login_secret}
    else:
        _flask_app.config["LOGIN_DISABLED"] = True
    if app.debug_web:
        threading.Thread(target=run_web_server, args=(app,)).start()
    else:
        threading.Thread(
            target=get_flask_app().run,
            daemon=True,
            args=(app.web_host, app.web_port),
        ).start()


# ── multi-account init ───────────────────────────────────────────


def init_web_multi(
    account_manager: "AccountManager",
    web_auth_manager: "WebAuthManager",
    loop: asyncio.AbstractEventLoop,
    web_host: str = "0.0.0.0",
    web_port: int = 5000,
    web_login_secret: str = "",
    start_account_cb=None,
    stop_account_cb=None,
    get_instance_cb=None,
):
    """Initialize web server with multi-account support."""
    global _account_manager, _web_auth_manager, _event_loop
    global web_login_users, _start_account_callback, _stop_account_callback
    global _get_instance_callback

    _account_manager = account_manager
    _web_auth_manager = web_auth_manager
    _event_loop = loop
    _start_account_callback = start_account_cb
    _stop_account_callback = stop_account_cb
    _get_instance_callback = get_instance_cb

    if web_login_secret:
        web_login_users = {"root": web_login_secret}
    else:
        _flask_app.config["LOGIN_DISABLED"] = True

    threading.Thread(
        target=get_flask_app().run,
        daemon=True,
        kwargs={"host": web_host, "port": web_port},
    ).start()


# ══════════════════════════════════════════════════════════════════
# ROUTES — Auth & Login
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = "root"
        web_login_form = {}
        for key, value in request.form.items():
            if value:
                value = deAesCrypt.decrypt(value)
            web_login_form[key] = value

        if not web_login_form.get("password"):
            return jsonify({"code": "0"})

        password = web_login_form["password"]
        if username in web_login_users and web_login_users[username] == password:
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        return jsonify({"code": "0"})

    return render_template("login.html")


# ══════════════════════════════════════════════════════════════════
# ROUTES — Dashboard (multi-account)
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/")
@login_required
def index():
    """Dashboard — show all accounts or legacy download view."""
    if _account_manager is None:
        return render_template(
            "index.html",
            download_state=(
                "pause"
                if get_download_state() is DownloadState.Downloading
                else "continue"
            ),
        )

    accounts = []
    for acc in _account_manager.list_accounts():
        accounts.append(acc.to_dict())

    return render_template("dashboard.html", accounts=accounts)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Account CRUD
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/add", methods=["POST"])
@login_required
def account_add():
    """Add a new account."""
    if _account_manager is None:
        return jsonify({"ok": False, "error": "Not in multi-account mode"}), 500

    data = request.get_json(silent=True) or {}
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")

    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "api_id and api_hash required"}), 400

    try:
        api_id = int(api_id)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "api_id must be a number"}), 400

    acc = _account_manager.add_account(api_id, str(api_hash))
    return jsonify({"ok": True, "account_id": acc.account_id})


@_flask_app.route("/account/<account_id>/remove", methods=["POST"])
@login_required
def account_remove(account_id: str):
    """Remove an account."""
    if _account_manager is None:
        return jsonify({"ok": False, "error": "Not in multi-account mode"}), 500

    if _stop_account_callback:
        try:
            _run_async(_stop_account_callback(account_id))
        except Exception:
            pass

    ok = _account_manager.remove_account(account_id)
    return jsonify({"ok": ok})


@_flask_app.route("/account/<account_id>/status")
@login_required
def account_status(account_id: str):
    """Get account status."""
    if _account_manager is None:
        return jsonify({"ok": False, "error": "Not in multi-account mode"}), 500

    acc = _account_manager.get_account(account_id)
    if not acc:
        return jsonify({"ok": False, "error": "Account not found"}), 404

    return jsonify({"ok": True, "account": acc.to_dict()})


# ══════════════════════════════════════════════════════════════════
# ROUTES — Account Auth (setup wizard)
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/setup")
@login_required
def account_setup(account_id: str):
    """Render the auth setup page."""
    if _account_manager is None:
        return redirect(url_for("index"))

    acc = _account_manager.get_account(account_id)
    if not acc:
        return redirect(url_for("index"))

    auth_status = {}
    if _web_auth_manager:
        auth_status = _web_auth_manager.get_status(account_id)

    step = auth_status.get("step", "phone")
    if acc.status == "authenticated" and step == "none":
        step = "success"

    return render_template(
        "setup.html",
        account_id=account_id,
        step=step,
        error=auth_status.get("error", ""),
        phone=auth_status.get("phone", acc.phone),
    )


@_flask_app.route("/account/<account_id>/auth/send_code", methods=["POST"])
@login_required
def auth_send_code(account_id: str):
    """Send verification code to phone."""
    if _web_auth_manager is None:
        return jsonify({"ok": False, "error": "Auth not available"}), 500

    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "step": "phone", "error": "Phone required"}), 400

    result = _run_async(_web_auth_manager.send_code(account_id, phone))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/auth/verify_code", methods=["POST"])
@login_required
def auth_verify_code(account_id: str):
    """Verify the SMS/Telegram code."""
    if _web_auth_manager is None:
        return jsonify({"ok": False, "error": "Auth not available"}), 500

    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "step": "code", "error": "Code required"}), 400

    result = _run_async(_web_auth_manager.verify_code(account_id, code))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/auth/verify_password", methods=["POST"])
@login_required
def auth_verify_password(account_id: str):
    """Verify 2FA password."""
    if _web_auth_manager is None:
        return jsonify({"ok": False, "error": "Auth not available"}), 500

    data = request.get_json(silent=True) or {}
    password = data.get("password", "").strip()
    if not password:
        return (
            jsonify({"ok": False, "step": "password", "error": "Password required"}),
            400,
        )

    result = _run_async(_web_auth_manager.verify_password(account_id, password))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/auth/status")
@login_required
def auth_status(account_id: str):
    """Poll auth status."""
    if _web_auth_manager is None:
        return jsonify({"step": "none", "error": ""})

    return jsonify(_web_auth_manager.get_status(account_id))


# ══════════════════════════════════════════════════════════════════
# ROUTES — Bot Token
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/bot/validate", methods=["POST"])
@login_required
def bot_validate(account_id: str):
    """Validate and save a bot token."""
    if _web_auth_manager is None:
        return jsonify({"ok": False, "error": "Auth not available"}), 500

    data = request.get_json(silent=True) or {}
    bot_token = data.get("bot_token", "").strip()
    if not bot_token:
        return jsonify({"ok": False, "error": "Bot token required"}), 400

    result = _run_async(_web_auth_manager.validate_bot_token(account_id, bot_token))
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Account Start/Stop
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/start", methods=["POST"])
@login_required
def account_start(account_id: str):
    """Start an authenticated account."""
    if _start_account_callback is None:
        return jsonify({"ok": False, "error": "Not available"}), 500

    try:
        result = _run_async(_start_account_callback(account_id))
        return jsonify({"ok": bool(result)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@_flask_app.route("/account/<account_id>/stop", methods=["POST"])
@login_required
def account_stop(account_id: str):
    """Stop a running account."""
    if _stop_account_callback is None:
        return jsonify({"ok": False, "error": "Not available"}), 500

    try:
        _run_async(_stop_account_callback(account_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# ROUTES — Legacy download status (preserved)
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/downloads")
@login_required
def downloads_page():
    """Legacy download status page."""
    return render_template(
        "index.html",
        download_state=(
            "pause" if get_download_state() is DownloadState.Downloading else "continue"
        ),
    )


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed"""
    return (
        '{ "download_speed" : "'
        + format_byte(get_total_download_speed())
        + '/s" , "upload_speed" : "0.00 B/s" } '
    )


@_flask_app.route("/set_download_state", methods=["POST"])
@login_required
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
        return "pause"

    if state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)
        return "continue"

    return state


@_flask_app.route("/get_app_version")
def get_app_version():
    """Get ld_tg_downloader version"""
    return utils.__version__


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """get download list"""
    if request.args.get("already_down") is None:
        return "[]"

    already_down = request.args.get("already_down") == "true"

    download_result = get_download_result()
    result = "["
    for chat_id, messages in download_result.items():
        for idx, value in messages.items():
            is_already_down = value["down_byte"] == value["total_size"]

            if already_down and not is_already_down:
                continue

            if result != "[":
                result += ","
            download_speed = format_byte(value["download_speed"]) + "/s"
            result += (
                '{ "chat":"'
                + f"{chat_id}"
                + '", "id":"'
                + f"{idx}"
                + '", "filename":"'
                + os.path.basename(value["file_name"])
                + '", "total_size":"'
                + f"{format_byte(value['total_size'])}"
                + '" ,"download_progress":"'
            )
            result += (
                f"{round(value['down_byte'] / value['total_size'] * 100, 1)}"
                + '" ,"download_speed":"'
                + download_speed
                + '" ,"save_path":"'
                + value["file_name"].replace("\\", "/")
                + '"}'
            )

    result += "]"
    return result


# ══════════════════════════════════════════════════════════════════
# ROUTES — Account page views (tasks / downloads / config / chats)
# ══════════════════════════════════════════════════════════════════


def _get_instance(account_id: str):
    """Get a running AccountInstance or None."""
    if _get_instance_callback is None:
        return None
    return _get_instance_callback(account_id)


@_flask_app.route("/account/<account_id>/tasks")
@login_required
def account_tasks_page(account_id: str):
    """Render the task management page."""
    instance = _get_instance(account_id)
    tasks = instance.web_get_tasks() if instance else []
    listen_forwards = instance.web_get_listen_forwards() if instance else []
    return render_template(
        "account_tasks.html",
        account_id=account_id,
        tasks=tasks,
        listen_forwards=listen_forwards,
    )


@_flask_app.route("/account/<account_id>/downloads")
@login_required
def account_downloads_page(account_id: str):
    """Render the download monitor page."""
    return render_template("account_downloads.html", account_id=account_id)


@_flask_app.route("/account/<account_id>/config")
@login_required
def account_config_page(account_id: str):
    """Render the config editor page."""
    instance = _get_instance(account_id)
    config = instance.web_get_config() if instance else {}
    return render_template("account_config.html", account_id=account_id, config=config)


@_flask_app.route("/account/<account_id>/chats")
@login_required
def account_chats_page(account_id: str):
    """Render the channel management page."""
    instance = _get_instance(account_id)
    chats = instance.web_get_chats() if instance else []
    return render_template("account_chats.html", account_id=account_id, chats=chats)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Task API (download / forward / listen / stop)
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/tasks/download", methods=["POST"])
@login_required
def task_download(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id required"}), 400

    result = _run_async(
        instance.web_download(
            chat_id_or_link=chat_id,
            start_id=int(data.get("start_message_id", 0)),
            end_id=int(data.get("end_message_id", 0)),
            download_filter=data.get("download_filter", ""),
        )
    )
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tasks/download_link", methods=["POST"])
@login_required
def task_download_link(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    link = data.get("link", "").strip()
    if not link:
        return jsonify({"ok": False, "error": "link required"}), 400

    result = _run_async(instance.web_download_link(link))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tasks/forward", methods=["POST"])
@login_required
def task_forward(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    from_chat = data.get("from_chat", "").strip()
    to_chat = data.get("to_chat", "").strip()
    if not from_chat or not to_chat:
        return jsonify({"ok": False, "error": "from_chat and to_chat required"}), 400

    result = _run_async(
        instance.web_forward(
            from_chat_link=from_chat,
            to_chat_link=to_chat,
            start_id=int(data.get("start_id", 0)),
            end_id=int(data.get("end_id", 0)),
            download_filter=data.get("filter", ""),
        )
    )
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tasks/listen_forward", methods=["POST"])
@login_required
def task_listen_forward(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    from_chat = data.get("from_chat", "").strip()
    to_chat = data.get("to_chat", "").strip()
    if not from_chat or not to_chat:
        return jsonify({"ok": False, "error": "from_chat and to_chat required"}), 400

    result = _run_async(
        instance.web_listen_forward(
            from_chat_link=from_chat,
            to_chat_link=to_chat,
            download_filter=data.get("filter", ""),
        )
    )
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tasks/stop_listen", methods=["POST"])
@login_required
def task_stop_listen(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400

    result = _run_async(instance.web_stop_listen(key))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tasks/list")
@login_required
def task_list(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": True, "tasks": []})

    return jsonify({"ok": True, "tasks": instance.web_get_tasks()})


@_flask_app.route("/account/<account_id>/tasks/listen_forwards")
@login_required
def task_listen_forwards(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": True, "listen_forwards": []})

    return jsonify({"ok": True, "listen_forwards": instance.web_get_listen_forwards()})


@_flask_app.route("/account/<account_id>/tasks/stop", methods=["POST"])
@login_required
def task_stop(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    if task_id is None:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    result = instance.web_stop_task(int(task_id))
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Config API
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/config/save", methods=["POST"])
@login_required
def config_save(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    result = instance.web_save_config(data)
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Chat (channel) management API
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/chats/add", methods=["POST"])
@login_required
def chat_add(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id required"}), 400

    result = instance.web_add_chat(chat_id, data.get("download_filter", ""))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/chats/remove", methods=["POST"])
@login_required
def chat_remove(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id required"}), 400

    result = instance.web_remove_chat(chat_id)
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Tools API
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/tools/get_info", methods=["POST"])
@login_required
def tools_get_info(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    chat_query = data.get("chat", "").strip()
    if not chat_query:
        return jsonify({"ok": False, "error": "chat required"}), 400

    result = _run_async(instance.web_get_chat_info(chat_query))
    if result.get("ok") and "chat" in result:
        result["info"] = result.pop("chat")
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Forward to comments
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/tasks/forward_to_comments", methods=["POST"])
@login_required
def task_forward_to_comments(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    from_chat = data.get("from_chat", "").strip()
    to_chat = data.get("to_chat", "").strip()
    if not from_chat or not to_chat:
        return jsonify({"ok": False, "error": "from_chat and to_chat required"}), 400

    result = _run_async(
        instance.web_forward_to_comments(
            from_chat_link=from_chat,
            to_chat_link=to_chat,
            start_id=int(data.get("start_id", 0)),
            end_id=int(data.get("end_id", 0)),
            download_filter=data.get("filter", ""),
        )
    )
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# ROUTES — Runtime filter / cleanup / forward management
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/tools/add_filter", methods=["POST"])
@login_required
def tools_add_filter(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    filter_str = data.get("filter", "").strip()
    if not filter_str:
        return jsonify({"ok": False, "error": "filter required"}), 400

    result = instance.web_add_filter(filter_str)
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tools/cleanup", methods=["POST"])
@login_required
def tools_toggle_cleanup(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if enabled is None:
        return jsonify({"ok": False, "error": "enabled (true/false) required"}), 400

    result = instance.web_toggle_cleanup(bool(enabled))
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tools/cleanup_status")
@login_required
def tools_cleanup_status(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_get_cleanup_status())


@_flask_app.route("/account/<account_id>/tools/forward_clean", methods=["POST"])
@login_required
def tools_forward_clean(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    result = instance.web_forward_clean()
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tools/forward_limit", methods=["POST"])
@login_required
def tools_forward_limit(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    data = request.get_json(silent=True) or {}
    size_str = data.get("size", "").strip()
    if not size_str:
        return jsonify({"ok": False, "error": "size required"}), 400

    result = instance.web_forward_limit(size_str)
    return jsonify(result)


@_flask_app.route("/account/<account_id>/tools/forward_limit_status")
@login_required
def tools_forward_limit_status(account_id: str):
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_get_forward_limit())


# ══════════════════════════════════════════════════════════════════
# ROUTES — Account-scoped download progress API
# ══════════════════════════════════════════════════════════════════


@_flask_app.route("/account/<account_id>/downloads/status")
@login_required
def downloads_status(account_id: str):
    """Summary: state, active count, total speed."""
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_get_downloads_status())


@_flask_app.route("/account/<account_id>/downloads/list")
@login_required
def downloads_list(account_id: str):
    """Per-file download list with progress, speed, ETA."""
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_get_downloads_list())


@_flask_app.route("/account/<account_id>/downloads/pause", methods=["POST"])
@login_required
def downloads_pause(account_id: str):
    """Pause downloads."""
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_pause_downloads())


@_flask_app.route("/account/<account_id>/downloads/continue", methods=["POST"])
@login_required
def downloads_continue(account_id: str):
    """Resume downloads."""
    instance = _get_instance(account_id)
    if not instance:
        return jsonify({"ok": False, "error": "Account not running"}), 404

    return jsonify(instance.web_continue_downloads())

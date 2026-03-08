"""
TraceLog 拾迹 - Memory Layer
基于 Markdown + JSON 的本地记忆系统
"""

import json
import os
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
POSTS_DIR = os.path.join(WORKSPACE_DIR, "posts")
PROFILE_PATH = os.path.join(WORKSPACE_DIR, "profile.md")
TODOS_PATH = os.path.join(WORKSPACE_DIR, "todos.json")
CONTEXT_POST_COUNT = 3


def init_workspace():
    """确保 workspace 目录结构存在。"""
    os.makedirs(POSTS_DIR, exist_ok=True)
    if not os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            f.write("# 用户画像\n\n（暂无数据，将在首次记忆整理后生成。）\n")
    if not os.path.exists(TODOS_PATH):
        with open(TODOS_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)


# 帖子

def _next_post_path() -> str:
    """生成下一个帖子文件路径，格式为 YYYYMMDD-NNN.md。"""
    now = datetime.now().astimezone()
    today = now.strftime("%Y%m%d")
    if not os.path.exists(POSTS_DIR):
        return os.path.join(POSTS_DIR, f"{today}-001.md")

    seqs = []
    for f in os.listdir(POSTS_DIR):
        if f.startswith(today + "-") and f.endswith(".md"):
            try:
                seqs.append(int(f.replace(".md", "").split("-")[1]))
            except ValueError:
                continue

    seq = (max(seqs) + 1) if seqs else 1
    return os.path.join(POSTS_DIR, f"{today}-{seq:03d}.md")


def save_post(user_input: str) -> str:
    """将用户输入保存为独立的帖子文件，并注入 YAML Frontmatter。返回 post_id。"""
    now = datetime.now().astimezone()
    iso_time = now.isoformat()

    path = _next_post_path()
    post_id = os.path.basename(path).replace(".md", "")

    frontmatter = f"---\nid: \"{post_id}\"\ndate: \"{iso_time}\"\ntype: \"post\"\n---\n\n"
    content = frontmatter + f"\n{user_input}\n"

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)

    return post_id


def read_recent_posts(count: int = CONTEXT_POST_COUNT) -> str:
    """读取最近 N 篇帖子内容，拼接为字符串。"""
    files = sorted(
        [f for f in os.listdir(POSTS_DIR) if f.endswith(".md")],
        reverse=True,
    ) if os.path.exists(POSTS_DIR) else []

    parts = []
    for fname in files[:count]:
        path = os.path.join(POSTS_DIR, fname)
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read().strip())

    parts.reverse()
    return "\n\n---\n\n".join(parts)


# 画像

def read_profile() -> str:
    """读取画像文件。"""
    if not os.path.exists(PROFILE_PATH):
        return ""
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_profile(content: str):
    """覆写画像文件。"""
    tmp = PROFILE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, PROFILE_PATH)


# 待办

def load_todos() -> list:
    if not os.path.exists(TODOS_PATH):
        return []
    try:
        with open(TODOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        print("[警告] todos.json 顶层结构不是数组，已作为空列表加载。")
        return []
    except json.JSONDecodeError:
        print("[警告] todos.json 格式损坏，已作为空列表加载。请检查文件！")
        return []
    except OSError as e:
        print(f"[警告] 读取 todos.json 失败：{e}，已作为空列表加载。")
        return []


def save_todos(todos: list):
    tmp = TODOS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TODOS_PATH)


def _next_todo_id() -> str:
    """生成唯一待办 ID，避免删除后序号复用导致碰撞。"""
    today = datetime.now().astimezone().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{today}-{short_uuid}"


def upsert_todos(existing: list, to_upsert: list, to_delete: list) -> list:
    """按 id 执行 UPSERT 和删除，返回更新后的列表。删除前校验 ID 是否实际存在。"""
    existing_ids = {t.get("id") for t in existing if t.get("id")}
    allowed_keys = {"task", "date", "start_time", "end_time", "status"}

    safe_delete_ids = set()
    for item in to_delete:
        tid = item.get("id")
        if not tid or tid not in existing_ids:
            print(f"[记忆] 忽略不存在的待办 id：{tid}")
            continue
        safe_delete_ids.add(tid)

    todos = [t for t in existing if t.get("id") not in safe_delete_ids]

    index = {t.get("id"): i for i, t in enumerate(todos) if t.get("id")}
    for item in to_upsert:
        if not isinstance(item, dict):
            continue

        tid = item.get("id")

        if tid and tid in index:
            # 只允许白名单字段进入持久化，避免 LLM 脏字段污染 todos.json
            for k in allowed_keys:
                if k in item:
                    todos[index[tid]][k] = item[k]
        elif tid and tid not in index:
            print(f"[记忆] 忽略未命中的待办更新 id：{tid}")
        else:
            # 新增任务必须具备 task，其他字段按白名单落盘
            task = item.get("task")
            if not isinstance(task, str) or not task.strip():
                continue
            new_item = {k: item.get(k) for k in allowed_keys if k in item}
            new_item["task"] = task.strip()
            new_item["id"] = _next_todo_id()
            todos.append(new_item)

    return todos


# 上下文组装

def read_posts_by_ids(post_ids: list[str]) -> str:
    """按 post_id 列表读取对应帖子文件，返回 --- 分隔的拼接内容。"""
    parts = []
    for pid in post_ids:
        path = os.path.join(POSTS_DIR, f"{pid}.md")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            parts.append(f.read().strip())
    return "\n\n---\n\n".join(parts)


def build_context(relevant_post_ids: list[str] | None = None) -> str:
    """拼接 profile + 近期帖子 + 相关帖子（可选，去重）+ 待办，供 LLM 阅读。"""
    sections = []

    profile = read_profile().strip()
    if profile and "暂无数据" not in profile:
        sections.append(profile)

    # 近期帖子（收集文件名 ID 用于去重）
    recent_files = sorted(
        [f for f in os.listdir(POSTS_DIR) if f.endswith(".md")],
        reverse=True,
    ) if os.path.exists(POSTS_DIR) else []
    recent_ids = {f.replace(".md", "") for f in recent_files[:CONTEXT_POST_COUNT]}

    posts = read_recent_posts()
    if posts:
        sections.append(f"# 近期帖子\n\n{posts}")

    # 相关帖子（语义检索结果，去重后追加）
    if relevant_post_ids:
        deduped = [pid for pid in relevant_post_ids if pid not in recent_ids]
        if deduped:
            relevant_posts = read_posts_by_ids(deduped)
            if relevant_posts:
                sections.append(f"# 相关帖子\n\n{relevant_posts}")

    todos = load_todos()
    pending = [t for t in todos if t.get("status") != "已完成"]
    if pending:
        def _fmt_todo(t):
            date_str = t.get('date') or '待定'
            start = t.get('start_time')
            end = t.get('end_time')
            if start and end:
                time_str = f" {start}~{end}"
            elif start:
                time_str = f" {start}"
            else:
                time_str = ""
            return f"- [{t.get('id', '?')}] {t['task']}（{date_str}{time_str}）"
        lines = [_fmt_todo(t) for t in pending]
        sections.append("# 待办事项\n\n" + "\n".join(lines))

    return "\n\n---\n\n".join(sections)

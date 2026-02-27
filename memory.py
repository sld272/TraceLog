"""
TraceLog 拾迹 - Memory Layer
基于 Markdown + JSON 的本地记忆系统
"""

import json
import os
from datetime import datetime

WORKSPACE_DIR = "workspace"
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


def save_post(user_input: str):
    """将用户输入保存为独立的帖子文件，并注入 YAML Frontmatter。"""
    now = datetime.now().astimezone()
    iso_time = now.isoformat() 
    
    path = _next_post_path()
    post_id = os.path.basename(path).replace(".md", "")
    
    # 构建标准的 YAML Frontmatter
    frontmatter = f"---\nid: \"{post_id}\"\ndate: \"{iso_time}\"\ntype: \"post\"\n---\n\n"
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter + f"\n{user_input}\n")


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
    with open(TODOS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_todos(todos: list):
    tmp = TODOS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TODOS_PATH)


def _next_todo_id(existing: list) -> str:
    """生成下一个待办 ID，格式为 YYYYMMDD-NNN。"""
    today = datetime.now().astimezone().strftime("%Y%m%d")
    seqs = [
        int(t["id"].split("-")[1])
        for t in existing
        if t.get("id", "").startswith(today + "-") and t["id"].split("-")[1].isdigit()
    ]
    seq = (max(seqs) + 1) if seqs else 1
    return f"{today}-{seq:03d}"


def upsert_todos(existing: list, to_upsert: list, to_delete: list) -> list:
    """按 id 执行 UPSERT 和删除，返回更新后的列表。删除前校验 ID 是否实际存在。"""
    existing_ids = {t.get("id") for t in existing if t.get("id")}

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
        tid = item.get("id")
        if tid and tid in index:
            todos[index[tid]].update(item)
        else:
            item["id"] = _next_todo_id(todos)
            todos.append(item)

    return todos


# 上下文组装

def build_context() -> str:
    """拼接 profile + 近期日记 + 任务列表，供 LLM 阅读。"""
    sections = []

    profile = read_profile().strip()
    if profile and "暂无数据" not in profile:
        sections.append(profile)

    posts = read_recent_posts()
    if posts:
        sections.append(f"# 近期帖子\n\n{posts}")

    todos = load_todos()
    pending = [t for t in todos if t.get("status") != "已完成"]
    if pending:
        def _fmt_todo(t):
            date_str = t.get('date') or '待定'
            time_str = f" {t.get('start_time')}~{t.get('end_time')}" if t.get('start_time') else ""
            return f"- [{t.get('id', '?')}] {t['task']}（{date_str}{time_str}）"
        lines = [_fmt_todo(t) for t in pending]
        sections.append("# 待办事项\n\n" + "\n".join(lines))

    return "\n\n---\n\n".join(sections)

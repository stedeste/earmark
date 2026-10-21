#!/usr/bin/env python3
"""
Earmark - Audio Metadata Manager (Phase 1)
A hybrid GUI + scripting tool for managing audio file metadata

New Features in Phase 1:
- Keyboard shortcuts (↑↓ navigate, Space play, Delete remove, Cmd+A select all, etc.)
- Data Link field type (link to external files)
- Markers field type with HH:MM:SS time ranges, sortable
- Multi-select files with checkboxes
- Batch operations panel
- Field-specific tags
- Help button showing all shortcuts

Usage:
    python earmark.py                 # Start web interface
    python earmark.py --script        # Interactive Python console
"""

from flask import Flask, render_template_string, request, jsonify, send_file, Response, send_from_directory
import sqlite3
import json
import os
import mimetypes
import shutil
import threading
import time
import hashlib
from pathlib import Path
from datetime import datetime
import sys

# A windowed app build (PyInstaller --windowed) has no console attached:
# give print() and Flask's startup messages somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# numpy powers the fast (vectorized + cached) waveform path. If it's missing we
# fall back to the original pure-Python decimation, so the app still runs.
try:
    import numpy as np
except Exception:
    np = None

app = Flask(__name__)

# Interface assets (Quadro stylesheet and fonts) live in the ui/ folder: next to
# this file when run from source, inside the unpacked bundle in a built app.
UI_DIR = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'ui')

@app.route('/ui/<path:filename>')
def ui_asset(filename):
    return send_from_directory(UI_DIR, filename)

# Database setup
# Databases live in ~/Documents/Earmark/ so Earmark works however it is
# launched (a bundled .app cannot write inside its own folder).
DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'Earmark')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'earmark.db')

def _resolve_db_path(name):
    """Bare names ('project.db') go in DATA_DIR; absolute and ~ paths are kept."""
    name = os.path.expanduser(name)
    if os.path.isabs(name):
        return name
    return os.path.join(DATA_DIR, name)

def init_db(db_path=None):
    """Initialize the SQLite database"""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # Files table
    c.execute('''CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        tags TEXT,
        created_at TEXT,
        UNIQUE(filepath)
    )''')
    
    # Fields table (custom metadata fields)
    c.execute('''CREATE TABLE IF NOT EXISTS fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        min_val REAL,
        max_val REAL,
        choices TEXT
    )''')
    
    # Field values table (field values for each file)
    # 'content' column stores indexed content for datalink fields (searchable)
    c.execute('''CREATE TABLE IF NOT EXISTS field_values (
        file_id INTEGER,
        field_id INTEGER,
        value TEXT,
        content TEXT,
        FOREIGN KEY(file_id) REFERENCES files(id),
        FOREIGN KEY(field_id) REFERENCES fields(id),
        PRIMARY KEY(file_id, field_id)
    )''')
    
    # Migration: Add content column if it doesn't exist (for existing databases)
    try:
        c.execute('ALTER TABLE field_values ADD COLUMN content TEXT')
        print("Database migrated: added 'content' column to field_values")
    except sqlite3.OperationalError:
        # Column already exists, ignore
        pass

    # Groups table (visual nestable folders for organising files).
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        parent_id INTEGER,
        FOREIGN KEY(parent_id) REFERENCES groups(id)
    )''')

    # Migration: add files.group_id column for existing databases.
    try:
        c.execute('ALTER TABLE files ADD COLUMN group_id INTEGER')
        print("Database migrated: added 'group_id' column to files")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

# Database helper class
class EarmarkDB:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        init_db(self.db_path)
    
    def add_file(self, filepath):
        """Add an audio file to the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        filename = os.path.basename(filepath)
        created_at = datetime.now().isoformat()
        
        try:
            c.execute('INSERT INTO files (filename, filepath, tags, created_at) VALUES (?, ?, ?, ?)',
                     (filename, filepath, '[]', created_at))
            conn.commit()
            file_id = c.lastrowid
            conn.close()
            return file_id
        except sqlite3.IntegrityError:
            conn.close()
            return None
    
    def delete_file(self, file_id):
        """Delete a file from the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM field_values WHERE file_id = ?', (file_id,))
        c.execute('DELETE FROM files WHERE id = ?', (file_id,))
        conn.commit()
        conn.close()
    
    def delete_files(self, file_ids):
        """Delete multiple files from the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        for file_id in file_ids:
            c.execute('DELETE FROM field_values WHERE file_id = ?', (file_id,))
            c.execute('DELETE FROM files WHERE id = ?', (file_id,))
        conn.commit()
        conn.close()
    
    def get_files(self):
        """Get all files"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT id, filename, filepath, tags, group_id FROM files ORDER BY filename')
        files = []
        for row in c.fetchall():
            files.append({
                'id': row[0],
                'filename': row[1],
                'filepath': row[2],
                'tags': json.loads(row[3]) if row[3] else [],
                'group_id': row[4],
            })
        conn.close()
        return files

    def get_file(self, file_id):
        """Get a single file with all its metadata"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT id, filename, filepath, tags, group_id FROM files WHERE id = ?', (file_id,))
        row = c.fetchone()

        if not row:
            conn.close()
            return None

        file_data = {
            'id': row[0],
            'filename': row[1],
            'filepath': row[2],
            'tags': json.loads(row[3]) if row[3] else [],
            'group_id': row[4],
            'fields': {}
        }
        
        # Get field values (including indexed content for datalink)
        c.execute('''SELECT f.name, v.value, f.type, v.content 
                    FROM field_values v 
                    JOIN fields f ON v.field_id = f.id 
                    WHERE v.file_id = ?''', (file_id,))
        
        for row in c.fetchall():
            file_data['fields'][row[0]] = row[1]
            # Store indexed content separately for datalink fields
            if row[2] == 'datalink' and row[3]:
                file_data['fields'][row[0] + '_content'] = row[3]
        
        conn.close()
        return file_data
    
    def add_tag(self, file_id, tag):
        """Add a tag to a file"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT tags FROM files WHERE id = ?', (file_id,))
        row = c.fetchone()
        
        if row:
            tags = json.loads(row[0]) if row[0] else []
            if tag not in tags:
                tags.append(tag)
                c.execute('UPDATE files SET tags = ? WHERE id = ?', (json.dumps(tags), file_id))
                conn.commit()
        
        conn.close()
    
    def add_tags_batch(self, file_ids, tag):
        """Add a tag to multiple files"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        for file_id in file_ids:
            c.execute('SELECT tags FROM files WHERE id = ?', (file_id,))
            row = c.fetchone()
            if row:
                tags = json.loads(row[0]) if row[0] else []
                if tag not in tags:
                    tags.append(tag)
                    c.execute('UPDATE files SET tags = ? WHERE id = ?', (json.dumps(tags), file_id))
        conn.commit()
        conn.close()
    
    def remove_tag(self, file_id, tag):
        """Remove a tag from a file"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT tags FROM files WHERE id = ?', (file_id,))
        row = c.fetchone()
        
        if row:
            tags = json.loads(row[0]) if row[0] else []
            if tag in tags:
                tags.remove(tag)
                c.execute('UPDATE files SET tags = ? WHERE id = ?', (json.dumps(tags), file_id))
                conn.commit()
        
        conn.close()
    
    def add_field(self, name, field_type, min_val=None, max_val=None, choices=None):
        """Add a custom field"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        choices_json = json.dumps(choices) if choices else None
        
        try:
            c.execute('INSERT INTO fields (name, type, min_val, max_val, choices) VALUES (?, ?, ?, ?, ?)',
                     (name, field_type, min_val, max_val, choices_json))
            conn.commit()
            field_id = c.lastrowid
            conn.close()
            return field_id
        except sqlite3.IntegrityError:
            conn.close()
            return None
    
    def delete_field(self, field_id):
        """Delete a field from the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('DELETE FROM field_values WHERE field_id = ?', (field_id,))
        c.execute('DELETE FROM fields WHERE id = ?', (field_id,))
        conn.commit()
        conn.close()

    def rename_field(self, field_id, new_name):
        """Rename a field. Returns True on success, False if name already taken or field missing."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        try:
            c.execute('UPDATE fields SET name = ? WHERE id = ?', (new_name, field_id))
            if c.rowcount == 0:
                conn.close()
                return False
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False

    # ----- Groups (visual nestable folders) -------------------------------
    def get_groups(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT id, name, parent_id FROM groups ORDER BY name')
        groups = [{'id': r[0], 'name': r[1], 'parent_id': r[2]} for r in c.fetchall()]
        conn.close()
        return groups

    def add_group(self, name, parent_id=None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('INSERT INTO groups (name, parent_id) VALUES (?, ?)', (name, parent_id))
        conn.commit()
        gid = c.lastrowid
        conn.close()
        return gid

    def rename_group(self, group_id, new_name):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('UPDATE groups SET name = ? WHERE id = ?', (new_name, group_id))
        ok = c.rowcount > 0
        conn.commit()
        conn.close()
        return ok

    def reparent_group(self, group_id, new_parent_id):
        """Move a group under a different parent. Refuses cycles (a group cannot become its own descendant)."""
        if new_parent_id is not None and new_parent_id == group_id:
            return False
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Walk up from new_parent_id; if we hit group_id, we'd create a cycle.
        if new_parent_id is not None:
            cur = new_parent_id
            visited = set()
            while cur is not None and cur not in visited:
                if cur == group_id:
                    conn.close()
                    return False
                visited.add(cur)
                c.execute('SELECT parent_id FROM groups WHERE id = ?', (cur,))
                row = c.fetchone()
                cur = row[0] if row else None
        c.execute('UPDATE groups SET parent_id = ? WHERE id = ?', (new_parent_id, group_id))
        ok = c.rowcount > 0
        conn.commit()
        conn.close()
        return ok

    def delete_group(self, group_id):
        """Delete a group; promote its child groups and member files up to its parent."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT parent_id FROM groups WHERE id = ?', (group_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return False
        parent_id = row[0]
        c.execute('UPDATE groups SET parent_id = ? WHERE parent_id = ?', (parent_id, group_id))
        c.execute('UPDATE files SET group_id = ? WHERE group_id = ?', (parent_id, group_id))
        c.execute('DELETE FROM groups WHERE id = ?', (group_id,))
        conn.commit()
        conn.close()
        return True

    def set_file_group(self, file_id, group_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('UPDATE files SET group_id = ? WHERE id = ?', (group_id, file_id))
        conn.commit()
        conn.close()

    def set_file_group_batch(self, file_ids, group_id):
        if not file_ids:
            return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.executemany('UPDATE files SET group_id = ? WHERE id = ?',
                      [(group_id, fid) for fid in file_ids])
        conn.commit()
        conn.close()
    
    def get_fields(self):
        """Get all fields"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('SELECT id, name, type, min_val, max_val, choices FROM fields')
        fields = []
        for row in c.fetchall():
            fields.append({
                'id': row[0],
                'name': row[1],
                'type': row[2],
                'min': row[3],
                'max': row[4],
                'choices': json.loads(row[5]) if row[5] else None
            })
        conn.close()
        return fields
    
    def set_value(self, file_id, field_id, value, content=None):
        """Set a field value for a file. Content is used for indexed/searchable text (datalink fields)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('INSERT OR REPLACE INTO field_values (file_id, field_id, value, content) VALUES (?, ?, ?, ?)',
                 (file_id, field_id, str(value), content))
        conn.commit()
        conn.close()
    
    def set_value_batch(self, file_ids, field_id, value):
        """Set a field value for multiple files"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        for file_id in file_ids:
            c.execute('INSERT OR REPLACE INTO field_values (file_id, field_id, value) VALUES (?, ?, ?)',
                     (file_id, field_id, str(value)))
        conn.commit()
        conn.close()
    
    def query(self, conditions):
        """
        Query files based on conditions
        Example: query([("H", ">", 15), ("R", "<", 5)])
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('SELECT id, filename, filepath, tags FROM files')
        files = []
        for row in c.fetchall():
            files.append({
                'id': row[0],
                'filename': row[1],
                'filepath': row[2],
                'tags': json.loads(row[3]) if row[3] else []
            })
        conn.close()
        return files

# Initialize database
db = EarmarkDB()

def read_file_content(filepath):
    """Read and return the text content of a file for indexing.
    Supports: .txt, .TextGrid, .csv, .md, .json, and other text formats.
    Returns None if file can't be read."""
    if not filepath or not os.path.exists(filepath):
        return None
    
    try:
        # Get file extension
        ext = os.path.splitext(filepath)[1].lower()
        
        # Try to read as text
        encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']
        
        for encoding in encodings:
            try:
                with open(filepath, 'r', encoding=encoding) as f:
                    content = f.read()
                    # Limit content size (store up to 100KB)
                    if len(content) > 100000:
                        content = content[:100000] + '\n[...truncated...]'
                    return content
            except UnicodeDecodeError:
                continue
        
        return None
    except Exception as e:
        print(f"Error reading file {filepath}: {e}")
        return None

# HTML Template with Phase 1 features
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Earmark</title>
    <style>
        :root {
            --bg-body: #1e293b;
            --bg-header: #334155;
            --bg-panel: #334155;
            --bg-panel-header: #1e293b;
            --bg-input: #475569;
            --bg-item: #475569;
            --bg-item-hover: #64748b;
            --bg-item-active: #3b82f6;
            --bg-item-selected: #1e40af;
            --border: #475569;
            --text-primary: #e2e8f0;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
        }
        
        [data-theme="light"] {
            --bg-body: #f1f5f9;
            --bg-header: #e2e8f0;
            --bg-panel: #ffffff;
            --bg-panel-header: #f8fafc;
            --bg-input: #e2e8f0;
            --bg-item: #f1f5f9;
            --bg-item-hover: #e2e8f0;
            --bg-item-active: #3b82f6;
            --bg-item-selected: #bfdbfe;
            --border: #cbd5e1;
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --text-muted: #94a3b8;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
        }
        
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: var(--bg-body);
            color: var(--text-primary);
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        
        .header {
            background: var(--bg-header);
            padding: 12px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 { font-size: 20px; }
        
        .btn {
            background: var(--accent);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        
        .btn:hover { background: var(--accent-hover); }
        .btn-secondary { background: var(--bg-input); color: var(--text-primary); }
        .btn-secondary:hover { background: var(--bg-item-hover); }
        .btn-danger { background: var(--rosso); }
        .btn-danger:hover { background: var(--rosso); }
        .btn-small { padding: 4px 8px; font-size: 11px; }
        
        .main {
            display: flex;
            flex: 1;
            overflow: hidden;
        }
        
        .panel {
            background: var(--bg-panel);
            border-right: 1px solid var(--border);
            overflow-y: auto;
        }
        
        .panel-left { width: 250px; }
        .panel-center { flex: 1; display: flex; flex-direction: column; }
        .panel-right { width: 300px; border-right: none; border-left: 1px solid var(--border); }
        
        .panel-header {
            padding: 12px;
            background: var(--bg-panel-header);
            border-bottom: 1px solid var(--border);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 12px;
            color: var(--text-secondary);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .panel-content { padding: 12px; flex: 1; overflow-y: auto; }
        
        .field-item, .file-item {
            padding: 8px;
            margin-bottom: 4px;
            background: var(--bg-item);
            border-radius: 4px;
            cursor: pointer;
        }
        
        .field-item:hover, .file-item:hover { background: var(--bg-item-hover); }
        
        .file-item.active {
            background: var(--bg-item-active);
            border-left: 4px solid var(--ardesia);
        }
        
        .file-item.selected {
            background: var(--bg-item-selected);
        }
        
        .file-item.focused {
            outline: 2px solid var(--ardesia);
            outline-offset: -2px;
        }
        
        .file-name { font-weight: 500; margin-bottom: 4px; }
        .file-tags { font-size: 12px; color: var(--inchiostro); }
        .file-duration {
            margin-left: 8px;
            flex-shrink: 0;
            font-size: 11px;
            font-variant-numeric: tabular-nums;
            color: var(--text-muted);
            opacity: 0.85;
        }

        /* ----- File-list groups (visual nestable folders) ----- */
        .group-row {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 6px 6px;
            margin: 2px 0;
            border-radius: 4px;
            cursor: pointer;
            user-select: none;
            color: var(--text-primary);
            background: transparent;
        }
        .group-row:hover { background: var(--bg-item-hover); }
        .group-chevron {
            display: inline-block;
            width: 14px;
            text-align: center;
            font-size: 10px;
            color: var(--text-secondary);
            transition: transform 0.1s ease;
        }
        .group-name {
            flex: 1;
            font-weight: 600;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .group-name[contenteditable="true"] {
            outline: 1px solid var(--accent);
            background: var(--bg-input);
            padding: 1px 4px;
            border-radius: 3px;
        }
        .group-count {
            font-size: 11px;
            color: var(--text-muted);
        }
        .group-actions {
            display: none;
            gap: 2px;
        }
        .group-row:hover .group-actions { display: inline-flex; }
        .group-actions button {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 12px;
            padding: 2px 4px;
            border-radius: 3px;
        }
        .group-actions button:hover { background: var(--bg-item); color: var(--text-primary); }
        .group-children { /* container for nested rows */ }
        .group-empty {
            font-size: 11px;
            color: var(--text-muted);
            padding: 4px 8px;
            font-style: italic;
        }
        
        .tag {
            display: inline-block;
            background: rgba(59, 130, 246, 0.2);
            border: 1px solid rgba(59, 130, 246, 0.3);
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            margin-right: 4px;
        }
        
        .tag-removable:hover {
            background: rgba(239, 68, 68, 0.3);
            border-color: rgba(239, 68, 68, 0.5);
        }
        
        .form-group {
            margin-bottom: 16px;
        }
        
        .form-group label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: var(--grafite);
            margin-bottom: 4px;
            text-transform: uppercase;
        }
        
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border);
            color: var(--text-primary);
            padding: 8px;
            border-radius: 4px;
            font-size: 14px;
        }
        
        .form-group textarea { resize: vertical; min-height: 80px; }
        
        .form-group.inactive {
            opacity: 0.5;
        }
        
        .form-group.inactive:hover {
            opacity: 0.8;
        }
        
        .form-group.inactive input,
        .form-group.inactive select,
        .form-group.inactive textarea {
            background: var(--intonaco);
        }
        
        input[type="range"] {
            width: 100%;
        }
        
        .value-display {
            font-size: 12px;
            color: var(--inchiostro);
            margin-top: 4px;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--grafite);
        }
        
        .tag-input-group {
            display: flex;
            gap: 8px;
            margin-top: 8px;
        }
        
        .tag-input-group input { flex: 1; }
        .tag-input-group button { flex-shrink: 0; }
        
        .tags-display {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-bottom: 8px;
        }
        
        .play-btn {
            background: var(--grigioverde);
            border: none;
            color: white;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            margin-right: 8px;
            flex-shrink: 0;
        }
        
        .play-btn:hover { background: var(--grigioverde-scuro); }
        
        /* Audio Player Bar - Inline in center panel */
        .player-bar-inline {
            background: var(--bg-panel-header);
            border-bottom: 1px solid var(--border);
            padding: 8px 12px;
            display: none;
        }
        
        .player-bar-inline.visible {
            display: block;
        }
        
        .player-resize-handle {
            height: 5px;
            background: transparent;
            cursor: row-resize;
            margin-top: 2px;
            position: relative;
        }
        
        .player-resize-handle:hover {
            background: var(--accent);
            opacity: 0.4;
            border-radius: 2px;
        }
        
        .player-controls-inline {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .player-info-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 4px;
        }
        
        .player-filename-inline {
            font-size: 11px;
            color: var(--text-secondary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .audio-info {
            font-size: 10px;
            color: var(--text-muted);
            font-family: monospace;
            white-space: nowrap;
        }
        
        .player-time-inline {
            font-family: monospace;
            color: var(--text-secondary);
            font-size: 11px;
            white-space: nowrap;
        }
        
        .player-progress-container {
            flex: 1;
            height: 40px;
            background: var(--calce);
            border-radius: 4px;
            position: relative;
            cursor: grab;
            overflow: hidden;
        }
        
        .waveform-canvas {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: 1;
            pointer-events: none;
        }
        
        .player-progress {
            height: 100%;
            background: rgba(59, 130, 246, 0.3);
            width: 0%;
            z-index: 2;
            position: relative;
        }
        
        .player-progress-container:hover .player-progress {
            background: rgba(59, 130, 246, 0.4);
        }
        
        .player-playhead {
            position: absolute;
            top: 0;
            bottom: 0;
            width: 2px;
            background: var(--calce);
            left: 0%;
            pointer-events: none;
            z-index: 5;
        }
        
        .player-markers {
            position: absolute;
            top: 0;
            bottom: 0;
            left: 0;
            right: 0;
            pointer-events: none;
            z-index: 3;
        }
        
        .player-marker {
            position: absolute;
            top: 0;
            bottom: 0;
            background: rgba(251, 191, 36, 0.4);
            border-left: 2px solid var(--ocra);
            cursor: pointer;
            /* Markers don't capture the pointer: plain-drag pans straight over them,
               and edge-dragging is handled by the container's Option-click handler. */
            pointer-events: none;
        }
        
        .player-marker:hover {
            background: rgba(251, 191, 36, 0.6);
        }
        
        .player-marker-label {
            position: absolute;
            top: 2px;
            left: 4px;
            font-size: 9px;
            color: var(--warn);
            white-space: nowrap;
            text-shadow: 0 0 2px #000;
        }
        
        .player-btn {
            background: var(--grigioverde);
            border: none;
            color: white;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 14px;
            flex-shrink: 0;
        }
        
        .player-btn:hover { background: var(--grigioverde-scuro); }
        
        .marker-quick-btn-inline {
            background: var(--ocra);
            padding: 4px 8px;
            border-radius: 4px;
            border: none;
            color: white;
            cursor: pointer;
            font-size: 11px;
            font-weight: 600;
            flex-shrink: 0;
        }
        
        .marker-quick-btn-inline:hover { background: var(--ocra); }

        /* Edit tool toggle (Grabber / Selector) */
        .tool-toggle {
            display: inline-flex;
            flex-shrink: 0;
            border: 1px solid var(--rule-soft);
            border-radius: 5px;
            overflow: hidden;
        }
        .tool-btn {
            background: var(--calce);
            border: none;
            color: var(--grafite);
            width: 26px;
            height: 26px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 700;
            font-family: Georgia, serif;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .tool-btn + .tool-btn { border-left: 1px solid var(--rule-soft); }
        .tool-btn:hover { background: var(--calce); color: var(--inchiostro); }
        .tool-btn.active { background: var(--grigioverde); color: var(--inchiostro); }
        .tool-name-display {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--ardesia);
            margin-right: 8px;
            flex-shrink: 0;
        }
        .player-progress-container.tool-selector { cursor: text; }
        .wave-selection {
            position: absolute;
            top: 0;
            bottom: 0;
            display: none;
            background: rgba(255, 255, 255, 0.16);
            border-left: 1px solid rgba(255, 255, 255, 0.85);
            border-right: 1px solid rgba(255, 255, 255, 0.85);
            pointer-events: none;
            z-index: 4;
        }
        [data-theme="light"] .wave-selection {
            background: rgba(30, 64, 175, 0.15);
            border-color: rgba(30, 64, 175, 0.8);
        }
        .wave-sel-info {
            align-items: center;
            gap: 4px;
            font-family: monospace;
            font-size: 10px;
            color: var(--text-primary);
            margin: 0 8px;
            white-space: nowrap;
        }
        .sel-btn {
            background: var(--calce);
            border: none;
            color: var(--inchiostro);
            border-radius: 3px;
            padding: 1px 6px;
            font-size: 10px;
            cursor: pointer;
        }
        .sel-btn:hover { background: var(--intonaco); }
        .zoom-btn.active { background: var(--grigioverde); color: var(--inchiostro); }

        /* Waveform zoom controls */
        .waveform-zoom-controls {
            display: flex;
            align-items: center;
            gap: 2px;
            flex-shrink: 0;
        }
        .zoom-btn {
            background: var(--calce);
            border: none;
            color: var(--inchiostro);
            width: 24px;
            height: 24px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .zoom-btn:hover { background: var(--intonaco); }
        .zoom-btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .zoom-level-display {
            font-family: monospace;
            font-size: 10px;
            color: var(--text-secondary);
            min-width: 34px;
            text-align: center;
        }

        /* Marker-line visibility selector */
        .marker-visibility-btn {
            background: var(--calce);
            border: none;
            color: var(--inchiostro);
            padding: 4px 8px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 11px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            flex-shrink: 0;
        }
        .marker-visibility-btn:hover { background: var(--intonaco); }
        .marker-visibility-menu {
            position: absolute;
            background: var(--intonaco);
            border: 1px solid var(--rule-soft);
            border-radius: 6px;
            padding: 6px;
            min-width: 180px;
            z-index: 50;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            display: none;
        }
        .marker-visibility-menu.visible { display: block; }
        .marker-visibility-menu label {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 4px 6px;
            font-size: 12px;
            color: var(--inchiostro);
            cursor: pointer;
            border-radius: 3px;
        }
        .marker-visibility-menu label:hover { background: var(--calce); }
        .marker-visibility-menu input[type="checkbox"] {
            width: 14px;
            height: 14px;
            cursor: pointer;
        }
        .marker-visibility-menu hr {
            border: none;
            border-top: 1px solid var(--rule-soft);
            margin: 4px 0;
        }
        .marker-visibility-menu .mv-swatch {
            width: 10px;
            height: 10px;
            border-radius: 2px;
            display: inline-block;
        }

        /* Linked text file viewer — floating panel (not modal) */
        .linked-file-panel {
            position: fixed;
            top: 100px;
            left: 200px;
            width: 600px;
            height: 500px;
            min-width: 300px;
            min-height: 200px;
            background: var(--intonaco);
            border: 1px solid var(--rule-soft);
            border-radius: 8px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            z-index: 500;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .linked-file-panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            border-bottom: 1px solid var(--rule-soft);
            background: var(--calce);
            cursor: move;
            user-select: none;
            border-radius: 8px 8px 0 0;
            flex-shrink: 0;
        }
        .linked-file-panel-title {
            font-size: 13px;
            color: var(--inchiostro);
            font-weight: 600;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
            margin-right: 12px;
        }
        .linked-file-panel-close {
            background: none;
            border: none;
            color: var(--grafite);
            font-size: 20px;
            cursor: pointer;
            padding: 0 6px;
            line-height: 1;
        }
        .linked-file-panel-close:hover { color: var(--inchiostro); }
        .linked-file-panel-path {
            font-size: 11px;
            color: var(--grafite);
            font-family: monospace;
            padding: 6px 12px;
            border-bottom: 1px solid var(--rule-soft);
            word-break: break-all;
            flex-shrink: 0;
        }
        .linked-file-panel-content {
            flex: 1;
            overflow: auto;
            padding: 12px;
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 12px;
            color: var(--inchiostro);
            white-space: pre-wrap;
            word-break: break-word;
            background: var(--intonaco);
        }
        .linked-file-panel-content.loading,
        .linked-file-panel-content.error {
            font-family: inherit;
            color: var(--grafite);
            text-align: center;
            padding: 40px 16px;
        }
        .linked-file-panel-content.error { color: var(--rosso); }
        .linked-file-panel-resize {
            position: absolute;
            right: 0;
            bottom: 0;
            width: 14px;
            height: 14px;
            cursor: nwse-resize;
            background: linear-gradient(135deg, transparent 50%, var(--intonaco) 50%, var(--intonaco) 60%, transparent 60%, transparent 70%, var(--intonaco) 70%, var(--intonaco) 80%, transparent 80%);
        }
        
        .file-checkbox {
            width: 18px;
            height: 18px;
            margin-right: 8px;
            cursor: pointer;
            flex-shrink: 0;
        }
        
        /* Batch operations panel */
        .batch-panel {
            background: var(--intonaco);
            border-top: 1px solid var(--rule-soft);
            padding: 12px;
            display: none;
        }
        
        .batch-panel.visible {
            display: block;
        }
        
        .batch-info {
            color: var(--ok);
            font-size: 14px;
            margin-bottom: 8px;
        }
        
        .batch-actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        
        /* Help modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }
        
        .modal-overlay.visible {
            display: flex;
        }
        
        .modal {
            background: var(--calce);
            border-radius: 8px;
            padding: 24px;
            max-width: 500px;
            max-height: 80vh;
            overflow-y: auto;
        }
        
        .modal h2 {
            margin-bottom: 16px;
            color: var(--inchiostro);
        }
        
        .shortcut-list {
            list-style: none;
        }
        
        .shortcut-list li {
            padding: 8px 0;
            border-bottom: 1px solid var(--rule-soft);
            display: flex;
            justify-content: space-between;
        }
        
        .shortcut-key {
            background: var(--calce);
            padding: 2px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 12px;
        }
        
        /* Markers display */
        .markers-list {
            max-height: 200px;
            overflow-y: auto;
            margin-bottom: 8px;
        }
        
        .marker-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 8px;
            background: var(--calce);
            border-radius: 4px;
            margin-bottom: 4px;
            font-size: 13px;
        }
        
        .marker-time {
            font-family: monospace;
            color: var(--ardesia);
        }
        
        .marker-label {
            flex: 1;
            margin-left: 8px;
            cursor: text;
            padding: 2px 4px;
            border-radius: 2px;
        }
        
        .marker-label:hover {
            background: var(--calce);
        }
        
        .marker-remove {
            background: none;
            border: none;
            color: var(--rosso);
            cursor: pointer;
            font-size: 16px;
            padding: 0 4px;
        }
        
        .marker-color-bar {
            width: 8px;
            align-self: stretch;
            border-radius: 2px 0 0 2px;
            margin: -8px;
            margin-right: 8px;
            cursor: pointer;
        }
        
        .marker-color-bar:hover {
            filter: brightness(1.2);
        }
        
        .marker-time {
            font-family: monospace;
            font-size: 12px;
            color: var(--grafite);
            cursor: ew-resize;
            padding: 2px 4px;
            border-radius: 2px;
            user-select: none;
        }
        
        .marker-time:hover {
            background: var(--calce);
            color: var(--inchiostro);
        }
        
        .marker-time-separator {
            color: var(--grafite);
            margin: 0 2px;
        }
        
        .marker-play-btn {
            background: none;
            border: none;
            color: var(--ardesia);
            cursor: pointer;
            font-size: 12px;
            padding: 2px 6px;
            opacity: 0.6;
        }
        
        .marker-play-btn:hover {
            opacity: 1;
        }
        
        .marker-color-select {
            background: var(--calce);
            border: 1px solid var(--rule-soft);
            color: var(--inchiostro);
            border-radius: 4px;
            padding: 2px 4px;
            font-size: 11px;
            cursor: pointer;
            margin-left: 8px;
        }
        
        /* Color picker popup */
        .color-picker-popup {
            position: absolute;
            background: var(--intonaco);
            border: 1px solid var(--rule-soft);
            border-radius: 8px;
            padding: 8px;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            z-index: 1000;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .color-picker-swatch {
            width: 24px;
            height: 24px;
            border-radius: 4px;
            cursor: pointer;
            border: 2px solid transparent;
        }
        
        .color-picker-swatch:hover {
            border-color: var(--rule);
            transform: scale(1.1);
        }
        
        .color-picker-swatch.selected {
            border-color: var(--rule);
        }
        
        .marker-input-row {
            display: flex;
            gap: 8px;
            margin-bottom: 8px;
        }
        
        .marker-input-row input {
            flex: 1;
        }
        
        .marker-input-row input.time-input {
            width: 90px;
            flex: none;
            font-family: monospace;
        }
        
        /* Resizable panels */
        .panel-resize-handle {
            width: 5px;
            cursor: col-resize;
            background: transparent;
            position: relative;
            flex-shrink: 0;
        }
        
        .panel-resize-handle:hover,
        .panel-resize-handle.dragging {
            background: var(--grigioverde);
        }
        
        .panel-resize-handle::after {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 3px;
            height: 30px;
            background: var(--intonaco);
            border-radius: 2px;
            opacity: 0;
        }
        
        .panel-resize-handle:hover::after {
            opacity: 1;
        }
        
        /* Data link display */
        .datalink-display {
            padding: 8px;
            background: var(--calce);
            border-radius: 4px;
            font-size: 13px;
            word-break: break-all;
        }
        
        .datalink-display a {
            color: var(--ardesia);
            text-decoration: none;
        }
        
        .datalink-display a:hover {
            text-decoration: underline;
        }
        
        /* Selection bar */
        .selection-bar {
            display: flex;
            gap: 8px;
            align-items: center;
            padding: 8px 12px;
            background: var(--intonaco);
            border-bottom: 1px solid var(--rule-soft);
        }
        
        .selection-count {
            color: var(--grafite);
            font-size: 13px;
        }
        
        /* Help button */
        .help-btn {
            background: var(--calce);
            border: none;
            color: var(--grafite);
            width: 24px;
            height: 24px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
        }
        
        .help-btn:hover {
            background: var(--intonaco);
            color: var(--inchiostro);
        }
        
        /* Dropdown menu */
        .dropdown {
            position: relative;
        }
        
        .dropdown-toggle {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        
        .dropdown-menu {
            position: absolute;
            top: 100%;
            left: 0;
            background: var(--intonaco);
            border: 1px solid var(--rule-soft);
            border-radius: 8px;
            min-width: 200px;
            padding: 8px 0;
            z-index: 1000;
            display: none;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .dropdown-menu.show {
            display: block;
        }
        
        .dropdown-item {
            padding: 8px 16px;
            cursor: pointer;
            color: var(--inchiostro);
        }
        
        .dropdown-item:hover {
            background: var(--calce);
        }
        
        .dropdown-divider {
            height: 1px;
            background: var(--calce);
            margin: 8px 0;
        }
        
        .dropdown-label {
            padding: 8px 16px;
            color: var(--grafite);
            font-size: 11px;
            text-transform: uppercase;
            font-weight: 600;
        }
        
        .db-name {
            color: var(--grafite);
            font-size: 13px;
            padding: 4px 8px;
            background: var(--calce);
            border-radius: 4px;
        }
        
        /* Speed control */
        .speed-control {
            display: flex;
            align-items: center;
            gap: 6px;
            color: var(--grafite);
            font-size: 12px;
        }
        
        .speed-control select {
            background: var(--calce);
            border: 1px solid var(--rule-soft);
            color: var(--inchiostro);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            cursor: pointer;
        }
        
        /* Drag and Drop overlay */
        .drop-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(59, 130, 246, 0.15);
            z-index: 2000;
            justify-content: center;
            align-items: center;
            pointer-events: none;
        }
        
        .drop-overlay.active {
            display: flex;
        }
        
        .drop-overlay-content {
            background: var(--bg-panel);
            border: 3px dashed var(--accent);
            border-radius: 16px;
            padding: 48px 64px;
            text-align: center;
        }
        
        .drop-overlay-content .drop-icon {
            font-size: 48px;
            margin-bottom: 16px;
        }
        
        .drop-overlay-content .drop-text {
            font-size: 18px;
            color: var(--text-primary);
        }
        
        .drop-overlay-content .drop-subtext {
            font-size: 13px;
            color: var(--text-secondary);
            margin-top: 8px;
        }
    </style>
    <link rel="stylesheet" href="/ui/quadro.css?v=1">
</head>
<body>
    <audio id="audio-player" style="display: none;"></audio>
    
    <!-- Drag and Drop Overlay -->
    <div class="drop-overlay" id="drop-overlay">
        <div class="drop-overlay-content">
            <div class="drop-icon">[+]</div>
            <div class="drop-text">Drop folder to import audio files</div>
            <div class="drop-subtext">Audio files will be imported from the dropped folder</div>
        </div>
    </div>
    
    <!-- Help Modal -->
    <div class="modal-overlay" id="help-modal" onclick="if(event.target === this) closeHelp()">
        <div class="modal shortcuts-modal">
            <h2>Shortcuts</h2>
            <div class="sc-head">
                <span class="sc-section">Keyboard</span>
                <span class="sc-tools">
                    <button class="btn btn-small" id="sc-reset-btn" onclick="resetShortcuts()" style="display:none;">Reset to defaults</button>
                    <button class="btn btn-small" id="sc-edit-btn" onclick="toggleShortcutEdit()">Customise</button>
                </span>
            </div>
            <div class="sc-note" id="sc-note"></div>
            <ul class="shortcut-list" id="shortcut-list-keys"></ul>
            <div class="sc-head"><span class="sc-section">Mouse and controls</span></div>
            <ul class="shortcut-list">
                <li><span>Extend selection (Selector)</span><span class="shortcut-key">⇧ + click</span></li>
                <li><span>Scroll zoomed waveform</span><span class="shortcut-key">Two-finger swipe ←/→</span></li>
                <li><span>Scroll / pan waveform</span><span class="shortcut-key">Drag (Grabber)</span></li>
                <li><span>Seek playhead</span><span class="shortcut-key">Click on waveform</span></li>
                <li><span>Scrub playhead</span><span class="shortcut-key">⌘ + drag</span></li>
                <li><span>Move marker edge</span><span class="shortcut-key">⌥ + drag near edge</span></li>
                <li><span>Zoom waveform (time)</span><span class="shortcut-key">⇧ + drag ←/→</span></li>
                <li><span>Zoom waveform (amplitude)</span><span class="shortcut-key">⇧ + drag ↑/↓</span></li>
                <li><span>Waveform / spectrogram / both</span><span class="shortcut-key">WAVE · SPECTRO keys</span></li>
                <li><span>Show / hide panels</span><span class="shortcut-key">AUDIO · FIELDS · FILES · INFO keys</span></li>
                <li><span>Swap two panels</span><span class="shortcut-key">Drag a title bar onto a panel</span></li>
            </ul>
            <button class="btn" style="margin-top: 16px; width: 100%;" onclick="closeHelp()">Close</button>
        </div>
    </div>

    <!-- New field dialog -->
    <div class="modal-overlay" id="field-modal" onclick="if(event.target === this) closeFieldDialog()">
        <div class="modal qd-dialog" role="dialog" aria-labelledby="field-modal-title">
            <div class="qd-dialog-bar">
                <span id="field-modal-title">New field</span>
                <button class="qd-dialog-close" onclick="closeFieldDialog()" title="Close (Esc)">×</button>
            </div>
            <div class="qd-dialog-body">
                <label class="qd-label" for="nf-name">Name</label>
                <input type="text" id="nf-name" placeholder="e.g. Speaker, Quality, Transcription" autocomplete="off">
                <div class="qd-label">Type</div>
                <div class="nf-types" id="nf-types">
                    <button type="button" class="nf-type" data-type="parameter"><span class="tool-lamp"></span><b>Parameter</b><small>A number in a range, set with a slider</small></button>
                    <button type="button" class="nf-type" data-type="text"><span class="tool-lamp"></span><b>Text</b><small>Free notes</small></button>
                    <button type="button" class="nf-type" data-type="choice"><span class="tool-lamp"></span><b>Choice</b><small>One option from a list</small></button>
                    <button type="button" class="nf-type" data-type="tags"><span class="tool-lamp"></span><b>Tags</b><small>Several labels</small></button>
                    <button type="button" class="nf-type" data-type="date"><span class="tool-lamp"></span><b>Date</b><small>A calendar date</small></button>
                    <button type="button" class="nf-type" data-type="markers"><span class="tool-lamp"></span><b>Markers</b><small>Time ranges on the audio</small></button>
                    <button type="button" class="nf-type" data-type="datalink"><span class="tool-lamp"></span><b>Data link</b><small>A linked file (transcription, analysis), searchable</small></button>
                </div>
                <div class="nf-opts" id="nf-opts-parameter">
                    <div class="nf-range">
                        <label><span class="qd-label">Min</span><input type="number" id="nf-min" value="0" step="any"></label>
                        <label><span class="qd-label">Max</span><input type="number" id="nf-max" value="10" step="any"></label>
                    </div>
                </div>
                <div class="nf-opts" id="nf-opts-choice">
                    <label class="qd-label" for="nf-choices">Options, one per line</label>
                    <textarea id="nf-choices" rows="4" placeholder="Interview&#10;Song&#10;Ambient"></textarea>
                </div>
                <div class="nf-error" id="nf-error"></div>
            </div>
            <div class="qd-dialog-keys">
                <button class="btn btn-secondary" onclick="closeFieldDialog()">Cancel</button>
                <button class="btn qd-commit" id="nf-create" onclick="submitFieldDialog()">Create field</button>
            </div>
        </div>
    </div>

  <!-- Linked file viewer (floating panel — not modal, main UI stays usable) -->
    <div class="linked-file-panel" id="linked-file-panel" style="display:none;">
        <div class="linked-file-panel-header" id="linked-file-drag-handle">
            <div class="linked-file-panel-title" id="linked-file-title">Linked file</div>
            <button class="linked-file-panel-close" onclick="closeLinkedFile()" title="Close">×</button>
        </div>
        <div class="linked-file-panel-path" id="linked-file-path"></div>
        <div class="linked-file-panel-content" id="linked-file-content">Loading…</div>
        <div class="linked-file-panel-resize" id="linked-file-resize" title="Drag to resize"></div>
    </div>

    <div class="header">
        <div style="display: flex; align-items: center; gap: 16px;">
            <h1>Earmark</h1>
            <div class="dropdown">
                <button class="btn btn-secondary dropdown-toggle" onclick="toggleDropdown('file-menu')">File ▾</button>
                <div class="dropdown-menu" id="file-menu">
                    <div class="dropdown-item" onclick="newDatabase()">New Database</div>
                    <div class="dropdown-item" onclick="openDatabase()">Open Database...</div>
                    <div class="dropdown-item" onclick="saveDatabaseAs()">Save Database As...</div>
                    <div class="dropdown-divider"></div>
                    <div class="dropdown-item" onclick="exportToCSV()">Export to CSV...</div>
                    <div class="dropdown-item" onclick="exportToExcel()">Export to Excel...</div>
                    <div class="dropdown-item" onclick="exportForMax()">Export for Max</div>
<div class="dropdown-divider"></div>
                    <div class="dropdown-label">Recent Databases</div>
                    <div id="recent-databases"></div>
                </div>
            </div>
            <span class="db-name" id="db-name-display" title="Current database">earmark.db</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
            <button class="btn" onclick="importFiles()">Import Audio</button>
            <button class="btn btn-secondary" onclick="linkMetadataFiles()">Link Metadata</button>
            <button class="btn btn-secondary" onclick="toggleQuery()">Query</button>
            <button class="btn btn-secondary" onclick="showConsole()">Console</button>
            <div class="view-keys" title="Show or hide panels. Drag a panel's title bar onto another panel to swap them.">
                <button class="view-key" id="view-key-wave" onclick="toggleView('wave')"><span class="tool-lamp"></span>Audio</button>
                <button class="view-key" id="view-key-fields" onclick="toggleView('fields')"><span class="tool-lamp"></span>Fields</button>
                <button class="view-key" id="view-key-files" onclick="toggleView('files')"><span class="tool-lamp"></span>Files</button>
                <button class="view-key" id="view-key-info" onclick="toggleView('info')"><span class="tool-lamp"></span>Info</button>
            </div>
            <button class="help-btn" onclick="toggleTheme()" title="Toggle day/night theme" id="theme-btn">Dark</button>
            <button class="help-btn" onclick="showHelp()" title="Keyboard shortcuts">?</button>
        </div>
    </div>
    
    <!-- Query Builder (hidden by default) -->
    <div id="query-panel" style="display: none; background: var(--intonaco); border-bottom: 1px solid var(--rule-soft); padding: 16px;">
        <div style="margin-bottom: 12px;">
            <div id="query-conditions">
                <div class="query-condition" style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
                    <select id="qfield-0" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                        <option value="">Select field...</option>
                    </select>
                    <select id="qop-0" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                        <option value="=">=</option>
                        <option value=">">></option>
                        <option value="<"><</option>
                        <option value="contains">contains</option>
                    </select>
                    <input type="text" id="qval-0" placeholder="value" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft); width: 150px;">
                </div>
            </div>
            <button class="btn" style="font-size: 12px; padding: 4px 12px;" onclick="addQueryCondition()">+ Add Condition</button>
        </div>
        <div style="display: flex; gap: 12px; align-items: center; padding-top: 12px; border-top: 1px solid var(--rule-soft);">
            <span style="color: var(--grafite); font-size: 14px;">Sort by:</span>
            <select id="sort-field" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                <option value="">Filename</option>
            </select>
            <select id="sort-order" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                <option value="asc">Ascending</option>
                <option value="desc">Descending</option>
            </select>
            <button class="btn" onclick="runQuery()" style="margin-left: auto;">Search</button>
            <button class="btn btn-secondary" onclick="clearQuery()">Clear</button>
        </div>
        <div id="query-results" style="margin-top: 12px; color: var(--ok); font-size: 14px;"></div>
    </div>
    
    <!-- Console Panel (hidden by default) -->
    <div id="console-panel" style="display: none; position: fixed; bottom: 0; left: 0; right: 0; height: 320px; background: var(--intonaco); border-top: 2px solid var(--ardesia); z-index: 9000; font-family: monospace; font-size: 13px;">
        <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; background: var(--calce); border-bottom: 1px solid var(--rule-soft);">
            <span style="color: var(--inchiostro); font-weight: 600;">Console</span>
            <div>
                <span onclick="consoleRunCmd('help')" style="color:var(--grafite);cursor:pointer;margin-right:8px;font-size:12px;">help</span>
                <span onclick="document.getElementById('console-output').innerHTML=''" style="color:var(--grafite);cursor:pointer;margin-right:8px;font-size:12px;">clear</span>
                <span onclick="document.getElementById('console-panel').style.display='none'" style="color:var(--grafite);cursor:pointer;font-size:16px;">&#10005;</span>
            </div>
        </div>
        <div id="console-output" style="flex: 1; overflow-y: auto; padding: 8px 12px; color: var(--inchiostro); white-space: pre-wrap; line-height: 1.5; height: 240px;"></div>
        <div style="display: flex; align-items: center; padding: 6px 12px; border-top: 1px solid var(--rule-soft); background: var(--calce);">
            <span style="color:var(--ardesia);margin-right:8px;">&gt;</span>
            <input id="console-input" type="text" onkeydown="consoleKeyDown(event)" style="flex:1; background:transparent; border:none; outline:none; color:var(--inchiostro); font-family:monospace; font-size:13px;" placeholder="stats FieldName / wordfreq FieldName 50 / help" autocomplete="off">
        </div>
    </div>
    
    <!-- Waveform strip (full width). Shown/hidden with the WAVE view key. -->
    <section class="wave-strip" id="wave-strip">
        <div class="panel-header wave-strip-bar"><span>Audio</span></div>
        <div class="wave-strip-empty" id="wave-strip-empty">Select a file and press Space (or ▶) to load it</div>
        <div class="player-bar-inline" id="player-bar">
            <div class="player-controls-inline">
                <button class="player-btn" id="player-play-btn" onclick="togglePlayPause()">▶</button>
                <div class="tool-toggle display-keys" title="Display: waveform, spectrogram, or both stacked">
                    <button class="tool-btn" id="disp-wave-btn" onclick="toggleDisplay('wave')"><span class="tool-lamp"></span>Wave</button>
                    <button class="tool-btn" id="disp-spec-btn" onclick="toggleDisplay('spec')"><span class="tool-lamp"></span>Spectro</button>
                </div>
                <div class="tool-toggle" title="Edit tool">
                    <button class="tool-btn" id="tool-selector-btn" onclick="setWaveTool('selector')" title="Selector (S): drag to select a range, click to place playhead"><span class="tool-lamp"></span>Select</button>
                    <button class="tool-btn active" id="tool-grabber-btn" onclick="setWaveTool('grabber')" title="Grabber (G): drag to scroll, click to seek"><span class="tool-lamp"></span>Grab</button>
                </div>
                <span class="player-time-inline"><span id="player-current-time">00:00</span> / <span id="player-duration">00:00</span></span>
                <label class="speed-glass" title="Playback speed">
                    <span class="speed-glass-cap">Speed</span>
                    <select id="playback-speed" onchange="changePlaybackSpeed(this.value)">
                        <option value="0.5">0.50×</option>
                        <option value="0.75">0.75×</option>
                        <option value="1" selected>1.00×</option>
                        <option value="1.25">1.25×</option>
                        <option value="1.5">1.50×</option>
                        <option value="2">2.00×</option>
                    </select>
                </label>
                <button class="zoom-btn" id="waveform-extras-toggle" onclick="toggleWaveformExtras()" title="Show/hide zoom &amp; marker controls" style="width:auto;padding:0 6px;font-size:11px;">▸</button>
                <div id="waveform-extras" style="display:none; align-items:center; gap:8px;">
                    <div class="waveform-zoom-controls" title="Zoom waveform (scroll wheel on waveform also works)">
                        <button class="zoom-btn" id="zoom-out-btn" onclick="zoomWaveform(1/1.5)" title="Zoom out">−</button>
                        <span class="zoom-level-display" id="zoom-level-display">1.0x</span>
                        <button class="zoom-btn" id="zoom-in-btn" onclick="zoomWaveform(1.5)" title="Zoom in">+</button>
                        <button class="zoom-btn" id="zoom-reset-btn" onclick="resetWaveformZoom()" title="Reset zoom" style="width:auto;padding:0 6px;font-size:10px;">1:1</button>
                        <button class="zoom-btn" id="wave-db-btn" onclick="toggleWaveformDb()" title="Amplitude scale" style="width:auto;padding:0 6px;font-size:10px;margin-left:4px;">Lin</button>
                    </div>
                    <div style="position: relative;">
                        <button class="marker-visibility-btn" id="marker-visibility-btn" onclick="toggleMarkerVisibilityMenu(event)" title="Choose which marker lines to display">
                            <span id="marker-visibility-label">Markers</span> ▾
                        </button>
                        <div class="marker-visibility-menu" id="marker-visibility-menu"></div>
                    </div>
                </div>
                <button class="marker-quick-btn-inline" onclick="dropMarkerAtPlayhead()" title="Drop marker (M)">M</button>
            </div>
            <div class="player-progress-container" id="player-progress-container" title="Grabber (G): drag scrolls · Selector (S): drag selects, M makes a marker · Click: seek · ⌘+drag: scrub playhead · ⌥+drag: move marker edge · ⇧+drag: zoom (←→ time, ↑↓ amplitude) · r/t: zoom in/out">
                    <canvas class="spectro-canvas" id="spectro-canvas"></canvas>
                    <canvas class="waveform-canvas" id="waveform-canvas"></canvas>
                    <div class="player-progress" id="player-progress"></div>
                    <div class="player-playhead" id="player-playhead"></div>
                    <div class="player-markers" id="player-markers"></div>
                    <div class="wave-selection" id="wave-selection"></div>
                </div>
            <div class="player-info-row">
                <span class="tool-name-display" id="tool-name-display" title="Current edit tool (G = Grabber, S = Selector)">Grabber</span>
                <span class="player-filename-inline" id="player-filename"></span>
                <span class="wave-sel-info" id="wave-sel-info" style="display:none;">
                    <span id="wave-sel-text"></span>
                    <button class="sel-btn" onclick="createMarkerFromSelection()" title="Create a marker from the selection (M)">+ Marker</button>
                    <button class="sel-btn" onclick="playSelection()" title="Play selection (Space)">▶</button>
                    <button class="sel-btn" onclick="zoomToSelection()" title="Zoom to selection">Zoom</button>
                    <button class="sel-btn" onclick="clearWaveSelection()" title="Clear selection (Esc)">×</button>
                </span>
                <span class="audio-info" id="audio-info"></span>
            </div>
            <div class="player-resize-handle" onmousedown="startPlayerResize(event)" title="Drag to resize waveform"></div>
        </div>
    </section>

    <div class="main" id="main-container">
<!-- Left Panel: Fields -->
        <div class="panel panel-left" id="panel-left">
            <div class="panel-header">
                <span>Fields</span>
                <div>
                    <button class="btn btn-small" onclick="newField()">+ New</button>
                </div>
            </div>
            <div style="padding: 8px; border-bottom: 1px solid var(--rule-soft);">
                <input type="text" id="field-search" placeholder="Search fields... (⌘⇧J)" 
                    style="width: 100%; background: var(--calce); border: 1px solid var(--rule-soft); color: var(--inchiostro); padding: 6px; border-radius: 4px; font-size: 13px;"
                    oninput="filterFields(this.value)">
            </div>
            <div class="panel-content" id="fields-list">
                <div class="empty-state">No fields yet</div>
            </div>
        </div>
        

        <!-- Center Panel: Files -->
        <div class="panel panel-center" id="panel-center">
            <div class="panel-header">
                <span>Files</span>
            </div>
            <div class="selection-bar">
                <input type="checkbox" id="select-all-checkbox" onchange="toggleSelectAll(this.checked)" style="width: 18px; height: 18px; cursor: pointer;">
                <span class="selection-count" id="selection-count">0 selected</span>
                <input type="text" id="file-search" placeholder="Search files... (⌘⇧K)" 
                    style="flex: 1; background: var(--calce); border: 1px solid var(--rule-soft); color: var(--inchiostro); padding: 6px; border-radius: 4px; font-size: 13px; margin-left: 8px;"
                    oninput="filterFiles(this.value)">
            </div>
            <div class="panel-content" id="files-list">
                <div class="empty-state">No files imported</div>
            </div>
            <!-- Batch Operations Panel -->
            <div class="batch-panel" id="batch-panel">
                <div class="batch-info" id="batch-info">0 files selected</div>
                <div class="batch-actions">
                    <button class="btn btn-small" onclick="batchAddTag()">+ Batch Tag</button>
                    <button class="btn btn-small btn-secondary" onclick="batchSetField()">Set Field</button>
                    <button class="btn btn-small btn-danger" onclick="batchDelete()">Delete Selected</button>
                </div>
            </div>
        </div>
        

        <!-- Right Panel: Metadata -->
        <div class="panel panel-right" id="panel-right">
            <div class="panel-header">
                <span id="selected-file-name">No file selected</span>
                <button id="file-stats-btn" class="btn btn-secondary" onclick="showFileStats()" style="display:none; font-size:11px; padding:2px 8px;">Stats</button>
            </div>
            <div style="padding: 8px; border-bottom: 1px solid var(--rule-soft);">
                <input type="text" id="content-search" placeholder="Search content... (⌘⇧L)" 
                    style="width: 100%; background: var(--calce); border: 1px solid var(--rule-soft); color: var(--inchiostro); padding: 6px; border-radius: 4px; font-size: 13px;">
            </div>
            <div class="panel-content" id="metadata-panel">
                <div class="empty-state">Select a file to edit metadata</div>
            </div>
        </div>
    </div>
    
    <script>
        // Groups (visual nestable folders)
        let allGroups = [];                 // [{id, name, parent_id}]
        let expandedGroupIds = new Set();   // group ids currently expanded in the tree

        let selectedFileId = null;
        let currentPlayingId = null;
        let audioPlayer = document.getElementById('audio-player');
        let queryConditionCount = 0;
        let allFiles = [];
        let allFields = [];
        let selectedFileIds = new Set();
        let focusedFileIndex = -1;
        let fileFilter = '';
        let fieldFilter = '';
        let currentMarkers = [];
        let currentMarkersFieldId = null;

        // Multi-field marker state: { [fieldId]: { name, markers, visible } }
        let markerFieldsState = {};
        // Waveform zoom state
        let waveformZoom = 1.0;       // 1.0 = full view; higher = zoomed in (horizontal / time)
        let waveformOffset = 0.0;     // fraction of total duration at left edge, 0..(1-1/zoom)
        let waveformVScale = 1.0;     // vertical (amplitude) zoom; 1 = normal
        let MAX_WAVEFORM_ZOOM = 64;     // raised per file once its sample count is known
        const MIN_WAVEFORM_ZOOM = 1;
        const MAX_WAVEFORM_VSCALE = 8;
        const MIN_WAVEFORM_VSCALE = 1;

        // Per-file durations (seconds), fetched lazily and shown in the file list.
        let fileDurations = {};

        // Undo/Redo state
        let undoStack = [];
        let redoStack = [];
        const MAX_UNDO = 50;
        
        function pushUndo(action) {
            undoStack.push(action);
            if (undoStack.length > MAX_UNDO) {
                undoStack.shift();
            }
            redoStack = []; // Clear redo stack on new action
            updateUndoRedoState();
        }
        
        function updateUndoRedoState() {
            // Could update UI indicators here
            console.log('Undo stack:', undoStack.length, 'Redo stack:', redoStack.length);
        }
        
        function undo() {
            if (undoStack.length === 0) {
                console.log('Nothing to undo');
                return;
            }
            
            const action = undoStack.pop();
            
            // Perform reverse action
            if (action.type === 'setValue') {
                // Restore previous value
                fetch('/api/file/' + action.fileId + '/value', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({field_id: action.fieldId, value: action.oldValue})
                }).then(() => {
                    redoStack.push({...action, oldValue: action.newValue, newValue: action.oldValue});
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            } else if (action.type === 'addTag') {
                // Remove the tag
                fetch('/api/file/' + action.fileId + '/tag', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tag: action.tag})
                }).then(() => {
                    redoStack.push(action);
                    loadFiles();
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            } else if (action.type === 'removeTag') {
                // Add the tag back
                fetch('/api/file/' + action.fileId + '/tag', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tag: action.tag})
                }).then(() => {
                    redoStack.push(action);
                    loadFiles();
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            }
        }
        
        function redo() {
            if (redoStack.length === 0) {
                console.log('Nothing to redo');
                return;
            }
            
            const action = redoStack.pop();
            
            // Perform the action again
            if (action.type === 'setValue') {
                fetch('/api/file/' + action.fileId + '/value', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({field_id: action.fieldId, value: action.oldValue})
                }).then(() => {
                    undoStack.push({...action, oldValue: action.newValue, newValue: action.oldValue});
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            } else if (action.type === 'addTag') {
                fetch('/api/file/' + action.fileId + '/tag', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tag: action.tag})
                }).then(() => {
                    undoStack.push(action);
                    loadFiles();
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            } else if (action.type === 'removeTag') {
                fetch('/api/file/' + action.fileId + '/tag', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tag: action.tag})
                }).then(() => {
                    undoStack.push(action);
                    loadFiles();
                    if (selectedFileId === action.fileId) {
                        loadFileMetadata(selectedFileId);
                    }
                    updateUndoRedoState();
                });
            }
        }
        
        // Panel resize state
        let isResizing = false;
        let resizeType = null;
        let startX = 0;
        let startLeftWidth = 0;
        let startRightWidth = 0;
        
        // Player bar height resize state
        let isResizingPlayer = false;
        let playerStartY = 0;
        let playerStartHeight = 0;
        
        // Resizing between two neighbouring panels. The flexible panel (Files,
        // or whichever takes the spare room) never gets a fixed width; the
        // other one of the pair is resized instead.
        var resizePrev = null, resizeNext = null, resizeHandleEl = null;
        function startResize(e, prevPanel, nextPanel, handle) {
            isResizing = true;
            resizePrev = prevPanel;
            resizeNext = nextPanel;
            resizeHandleEl = handle;
            startX = e.clientX;
            startLeftWidth = prevPanel.offsetWidth;
            startRightWidth = nextPanel.offsetWidth;
            handle.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        }
        
        function startPlayerResize(e) {
            isResizingPlayer = true;
            playerStartY = e.clientY;
            playerStartHeight = document.getElementById('player-progress-container').offsetHeight;
            document.body.style.cursor = 'row-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        }
        
        document.addEventListener('mousemove', function(e) {
            if (!isResizing && !isResizingPlayer) return;
            
            if (isResizing) {
                const dx = e.clientX - startX;
                var maxW = Math.max(300, window.innerWidth - 360);
                if (resizePrev.classList.contains('is-flex')) {
                    var w = Math.max(160, Math.min(maxW, startRightWidth - dx));
                    resizeNext.style.width = w + 'px';
                    uiLayout.widths[resizeNext.id] = w;
                } else {
                    var w2 = Math.max(160, Math.min(maxW, startLeftWidth + dx));
                    resizePrev.style.width = w2 + 'px';
                    uiLayout.widths[resizePrev.id] = w2;
                }
            }
            
            if (isResizingPlayer) {
                const dy = e.clientY - playerStartY;
                const container = document.getElementById('player-progress-container');
                // Allow the waveform to be as tall as the user drags. The lower bound
                // keeps it visible; the upper bound is the viewport height minus a small
                // margin so the player bar can never fully consume the window.
                const maxH = Math.max(200, window.innerHeight - 200);
                const newHeight = Math.max(30, Math.min(maxH, playerStartHeight + dy));
                container.style.height = newHeight + 'px';
                // Live redraw so the waveform fills the new height immediately
                if (currentWaveformPeaks) {
                    drawWaveform(currentWaveformPeaks);
                    updatePlayerMarkers();
                    updatePlayheadPosition();
                }
            }
        });
        
        document.addEventListener('mouseup', function() {
            if (isResizing) {
                isResizing = false;
                if (resizeHandleEl) resizeHandleEl.classList.remove('dragging');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                resizeType = null;
                saveLayout();

                // Redraw waveform after panel resize
                if (currentWaveformPeaks) {
                    setTimeout(() => {
                        drawWaveform(currentWaveformPeaks);
                        updatePlayerMarkers();
                        updatePlayheadPosition();
                    }, 50);
                }
            }
            
            // Handle player bar height resize
            if (isResizingPlayer) {
                isResizingPlayer = false;
                uiLayout.waveHeight = document.getElementById('player-progress-container').offsetHeight;
                saveLayout();
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                
                if (currentWaveformPeaks) {
                    setTimeout(() => {
                        drawWaveform(currentWaveformPeaks);
                        updatePlayerMarkers();
                        updatePlayheadPosition();
                    }, 50);
                }
            }
        });
        
        // ---- Display: waveform, spectrogram, or both (stacked, spectrogram on top) ----
        function displayLanes(container) {
            var d = (typeof uiLayout !== 'undefined' && uiLayout.display) ? uiLayout.display : { wave: true, spec: false };
            var h = container.offsetHeight;
            if (d.wave && d.spec) {
                var specH = Math.round(h * 0.5);
                return { specTop: 0, specH: specH, waveTop: specH, waveH: h - specH };
            }
            if (d.spec) return { specTop: 0, specH: h, waveTop: 0, waveH: h };
            return { specTop: 0, specH: 0, waveTop: 0, waveH: h };
        }

        function applyDisplayClasses() {
            var d = uiLayout.display;
            var c = document.getElementById('player-progress-container');
            if (c) {
                c.classList.toggle('disp-wave', !!d.wave);
                c.classList.toggle('disp-spec', !!d.spec);
            }
            var wb = document.getElementById('disp-wave-btn');
            var sb = document.getElementById('disp-spec-btn');
            if (wb) wb.classList.toggle('active', !!d.wave);
            if (sb) sb.classList.toggle('active', !!d.spec);
        }

        function toggleDisplay(which) {
            var d = uiLayout.display;
            var other = which === 'wave' ? 'spec' : 'wave';
            if (d[which] && !d[other]) return;            // keep at least one view on
            d[which] = !d[which];
            // Two lanes need room: give the strip a sensible height the first time.
            if (d.wave && d.spec && uiLayout.waveHeight < 240) {
                uiLayout.waveHeight = 280;
                document.getElementById('player-progress-container').style.height = '280px';
            }
            saveLayout();
            applyDisplayClasses();
            redrawWaveSoon();
        }

        var spectroState = { fileId: null, data: null, img: null, pending: false, timer: null, seq: 0, error: null };

        // Amber display-glass colour ramp: dark glass -> amber -> pale phosphor
        var SPECTRO_LUT = (function () {
            var stops = [[0, 18, 22, 19], [0.35, 70, 46, 14], [0.62, 176, 112, 32], [0.82, 240, 168, 58], [1, 255, 240, 205]];
            var lut = new Uint8ClampedArray(256 * 3);
            for (var i = 0; i < 256; i++) {
                var t = i / 255, k = 0;
                while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
                var a = stops[k], b = stops[k + 1], u = (t - a[0]) / (b[0] - a[0]);
                for (var ch = 0; ch < 3; ch++) lut[i * 3 + ch] = a[ch + 1] + (b[ch + 1] - a[ch + 1]) * u;
            }
            return lut;
        })();

        function spectroToImage(res) {
            var bin = atob(res.data), cols = res.cols, rows = res.rows;
            var off = document.createElement('canvas');
            off.width = cols; off.height = rows;
            var octx = off.getContext('2d');
            var im = octx.createImageData(cols, rows);
            var px = im.data;
            for (var c = 0; c < cols; c++) {
                for (var r = 0; r < rows; r++) {
                    var v = bin.charCodeAt(c * rows + r);
                    var o = ((rows - 1 - r) * cols + c) * 4;      // high frequencies at the top
                    px[o] = SPECTRO_LUT[v * 3]; px[o + 1] = SPECTRO_LUT[v * 3 + 1]; px[o + 2] = SPECTRO_LUT[v * 3 + 2]; px[o + 3] = 255;
                }
            }
            octx.putImageData(im, 0, 0);
            return off;
        }

        // Ask for exactly the device pixels of the lane (Retina included).
        function requestSpectro(viewStart, viewEnd, devW, devH) {
            var cols = Math.max(64, Math.min(4096, Math.round(devW)));
            var rows = Math.max(64, Math.min(1024, Math.round(devH)));
            var reqKey = currentWaveformFileId + '|' + viewStart.toFixed(6) + '|' + viewEnd.toFixed(6) + '|' + cols + '|' + rows;
            if (reqKey === spectroState.lastReq) return;      // already asked for exactly this
            if (spectroState.timer) clearTimeout(spectroState.timer);
            spectroState.timer = setTimeout(function () {
                spectroState.timer = null;
                var fid = currentWaveformFileId;
                if (!fid) return;
                spectroState.lastReq = reqKey;
                var seq = ++spectroState.seq;
                fetch('/api/audio/' + fid + '/spectrogram?cols=' + cols + '&rows=' + rows + '&start=' + viewStart + '&end=' + viewEnd)
                    .then(function (r) { return r.json(); })
                    .then(function (res) {
                        if (seq !== spectroState.seq || fid !== currentWaveformFileId) return;
                        if (res.error) { spectroState.error = res.error; spectroState.data = null; spectroState.img = null; }
                        else { spectroState.error = null; spectroState.data = res; spectroState.img = spectroToImage(res); }
                        drawSpectrogram();
                    })
                    .catch(function () {});
            }, 120);
        }

        function drawSpectrogram() {
            var canvas = document.getElementById('spectro-canvas');
            if (!canvas) return;
            var d = uiLayout.display;
            if (!d.spec) return;
            if (spectroState.fileId !== currentWaveformFileId) {
                spectroState.fileId = currentWaveformFileId;
                spectroState.data = null; spectroState.img = null; spectroState.error = null; spectroState.lastReq = null;
            }
            var container = canvas.parentElement;
            var lane = displayLanes(container);
            var dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
            var cssW = container.offsetWidth;
            var W = Math.max(1, Math.round(cssW * dpr));
            var H = Math.max(1, Math.round(lane.specH * dpr));
            canvas.width = W; canvas.height = H;
            canvas.style.width = cssW + 'px';
            canvas.style.height = lane.specH + 'px';
            canvas.style.top = lane.specTop + 'px';
            var ctx = canvas.getContext('2d');
            ctx.fillStyle = '#121613';
            ctx.fillRect(0, 0, W, H);
            if (!currentWaveformFileId) return;

            var viewStart = waveformOffset, viewEnd = Math.min(1, waveformOffset + 1 / waveformZoom);
            var res = spectroState.data;
            if (res && spectroState.img) {
                var span = res.window[1] - res.window[0];
                var sx = (viewStart - res.window[0]) / span * res.cols;
                var sw = (viewEnd - viewStart) / span * res.cols;
                ctx.imageSmoothingEnabled = true;
                ctx.imageSmoothingQuality = 'high';
                ctx.drawImage(spectroState.img, sx, 0, sw, res.rows, 0, 0, W, H);
                // Frequency labels on the glass
                ctx.font = (9 * dpr) + 'px "B612 Mono", Menlo, monospace';
                ctx.fillStyle = 'rgba(184, 132, 51, 0.9)';
                var marks = [100, 1000, 10000];
                for (var i = 0; i < marks.length; i++) {
                    var f = marks[i];
                    if (f <= res.fmin || f >= res.fmax) continue;
                    var y = H - (Math.log(f / res.fmin) / Math.log(res.fmax / res.fmin)) * H;
                    if (y < 12 * dpr || y > H - 4 * dpr) continue;    // too close to an edge to read
                    ctx.fillRect(0, y, 5 * dpr, 1);
                    ctx.fillText(f >= 1000 ? (f / 1000) + 'k' : String(f), 7 * dpr, y + 3 * dpr);
                }
                var covers = viewStart >= res.window[0] - 1e-9 && viewEnd <= res.window[1] + 1e-9;
                var sharp = sw >= Math.min(4096, W) * 0.9 && res.rows >= Math.min(1024, H) * 0.9;
                if (!covers || !sharp) requestSpectro(viewStart, viewEnd, W, H);
            } else if (spectroState.error) {
                ctx.font = (11 * dpr) + 'px "Archivo", sans-serif';
                ctx.fillStyle = 'rgba(184, 132, 51, 0.9)';
                ctx.fillText(spectroState.error, 10 * dpr, 18 * dpr);
            } else {
                requestSpectro(viewStart, viewEnd, W, H);
            }
        }

        // Every waveform redraw (zoom, scroll, resize, new file) also redraws the spectrogram.
        var _drawWaveformOnly = drawWaveform;
        drawWaveform = function (peaks) {
            _drawWaveformOnly(peaks);
            drawSpectrogram();
        };

        // ---- Layout: which panels show, their order and sizes (remembered) ----
        var VIEW_IDS = { wave: 'wave-strip', fields: 'panel-left', files: 'panel-center', info: 'panel-right' };
        var uiLayout = {
            show: { wave: true, fields: true, files: true, info: true },
            order: ['panel-left', 'panel-center', 'panel-right'],
            widths: {},
            waveHeight: 180,
            display: { wave: true, spec: false }
        };
        (function loadLayout() {
            try {
                var saved = JSON.parse(localStorage.getItem('earmark_layout') || 'null');
                if (saved) {
                    if (saved.show) for (var k in uiLayout.show) if (typeof saved.show[k] === 'boolean') uiLayout.show[k] = saved.show[k];
                    if (saved.order && saved.order.length === 3) {
                        var ok = uiLayout.order.every(function (id) { return saved.order.indexOf(id) !== -1; });
                        if (ok) uiLayout.order = saved.order;
                    }
                    if (saved.widths) uiLayout.widths = saved.widths;
                    if (saved.waveHeight) uiLayout.waveHeight = saved.waveHeight;
                    if (saved.display && (saved.display.wave || saved.display.spec)) {
                        uiLayout.display = { wave: !!saved.display.wave, spec: !!saved.display.spec };
                    }
                }
            } catch (e) {}
        })();

        function saveLayout() {
            try { localStorage.setItem('earmark_layout', JSON.stringify(uiLayout)); } catch (e) {}
        }

        function redrawWaveSoon() {
            var hasWave = false;
            try { hasWave = !!currentWaveformPeaks; } catch (e) { hasWave = false; }  // not declared yet on first layout
            if (hasWave) {
                setTimeout(function () {
                    drawWaveform(currentWaveformPeaks);
                    updatePlayerMarkers();
                    updatePlayheadPosition();
                }, 30);
            }
        }

        function layoutPanels() {
            var main = document.getElementById('main-container');
            var handles = main.querySelectorAll('.panel-resize-handle');
            for (var h = 0; h < handles.length; h++) handles[h].parentNode.removeChild(handles[h]);

            var visible = [];
            for (var i = 0; i < uiLayout.order.length; i++) {
                var panel = document.getElementById(uiLayout.order[i]);
                main.appendChild(panel);              // re-append in the saved order
                var key = null;
                for (var v in VIEW_IDS) if (VIEW_IDS[v] === panel.id) key = v;
                var on = uiLayout.show[key] !== false;
                panel.classList.toggle('is-hidden', !on);
                panel.classList.remove('is-flex');
                if (on) visible.push(panel);
            }
            // One panel takes the spare room: Files if shown, otherwise the widest visible one
            var flex = null;
            var files = document.getElementById('panel-center');
            if (visible.indexOf(files) !== -1) flex = files;
            else if (visible.length) flex = visible[visible.length - 1];
            for (var j = 0; j < visible.length; j++) {
                var pnl = visible[j];
                if (pnl === flex) {
                    pnl.classList.add('is-flex');
                    pnl.style.width = '';
                } else {
                    var w = uiLayout.widths[pnl.id];
                    pnl.style.width = (w ? w : (pnl.id === 'panel-right' ? 300 : 250)) + 'px';
                }
                pnl.style.flex = '';
                if (j > 0) {
                    var handle = document.createElement('div');
                    handle.className = 'panel-resize-handle';
                    (function (prev, next, hd) {
                        hd.onmousedown = function (e) { startResize(e, prev, next, hd); };
                    })(visible[j - 1], pnl, handle);
                    main.insertBefore(handle, pnl);
                }
            }
            document.getElementById('main-container').classList.toggle('is-empty', visible.length === 0);
            document.getElementById('wave-strip').classList.toggle('is-hidden', !uiLayout.show.wave);
            document.getElementById('player-progress-container').style.height = uiLayout.waveHeight + 'px';
            for (var key2 in VIEW_IDS) {
                var btn = document.getElementById('view-key-' + key2);
                if (btn) btn.classList.toggle('active', uiLayout.show[key2] !== false);
            }
            redrawWaveSoon();
        }

        function toggleView(key) {
            uiLayout.show[key] = !(uiLayout.show[key] !== false);
            saveLayout();
            layoutPanels();
        }

        // Drag a panel's title bar onto another panel to swap their places.
        var panelDrag = null;
        document.addEventListener('mousedown', function (e) {
            var bar = e.target.closest ? e.target.closest('#main-container > .panel > .panel-header') : null;
            if (!bar || e.button !== 0) return;
            if (e.target.closest('button, input, select, a, [contenteditable="true"]')) return;
            panelDrag = { panel: bar.parentNode, x: e.clientX, y: e.clientY, active: false, target: null };
        });
        document.addEventListener('mousemove', function (e) {
            if (!panelDrag) return;
            if (!panelDrag.active) {
                if (Math.abs(e.clientX - panelDrag.x) + Math.abs(e.clientY - panelDrag.y) < 6) return;
                panelDrag.active = true;
                panelDrag.panel.classList.add('is-dragging');
                document.body.style.cursor = 'grabbing';
                document.body.style.userSelect = 'none';
            }
            var el = document.elementFromPoint(e.clientX, e.clientY);
            var over = el && el.closest ? el.closest('#main-container > .panel') : null;
            if (over === panelDrag.panel) over = null;
            if (panelDrag.target && panelDrag.target !== over) panelDrag.target.classList.remove('is-drop-target');
            panelDrag.target = over;
            if (over) over.classList.add('is-drop-target');
        });
        document.addEventListener('mouseup', function () {
            if (!panelDrag) return;
            var d = panelDrag;
            panelDrag = null;
            if (!d.active) return;
            d.panel.classList.remove('is-dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            if (d.target) {
                d.target.classList.remove('is-drop-target');
                var a = uiLayout.order.indexOf(d.panel.id), b = uiLayout.order.indexOf(d.target.id);
                if (a !== -1 && b !== -1) {
                    uiLayout.order[a] = d.target.id;
                    uiLayout.order[b] = d.panel.id;
                    saveLayout();
                    layoutPanels();
                }
            }
        });

        layoutPanels();
        applyDisplayClasses();

        // Change marker color
        function changeMarkerColor(fieldId, markerIndex, newColor) {
            fetch('/api/file/' + selectedFileId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === fieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    if (markers[markerIndex]) {
                        markers[markerIndex].color = newColor;
                    }
                    
                    fetch('/api/file/' + selectedFileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        currentMarkers = markers;
                        if (markerFieldsState[fieldId]) markerFieldsState[fieldId].markers = markers;
                        updatePlayerMarkers();
                        loadFileMetadata(selectedFileId);
                    });
                });
        }
        
        // Show color picker popup (Pro Tools style)
        let activeColorPicker = null;
        
        function showColorPicker(fieldId, markerIndex, element) {
            // Remove existing picker
            hideColorPicker();
            
            const rect = element.getBoundingClientRect();
            const picker = document.createElement('div');
            picker.className = 'color-picker-popup';
            picker.id = 'color-picker-popup';
            picker.style.left = rect.right + 5 + 'px';
            picker.style.top = rect.top + 'px';
            
            markerColors.forEach(c => {
                const swatch = document.createElement('div');
                swatch.className = 'color-picker-swatch';
                swatch.style.background = c.border;
                swatch.title = c.name;
                swatch.onclick = () => {
                    changeMarkerColor(fieldId, markerIndex, c.name);
                    hideColorPicker();
                };
                picker.appendChild(swatch);
            });
            
            document.body.appendChild(picker);
            activeColorPicker = picker;
            
            // Close on click outside
            setTimeout(() => {
                document.addEventListener('click', hideColorPickerOnClickOutside);
            }, 10);
        }
        
        function hideColorPicker() {
            if (activeColorPicker) {
                activeColorPicker.remove();
                activeColorPicker = null;
            }
            document.removeEventListener('click', hideColorPickerOnClickOutside);
        }
        
        function hideColorPickerOnClickOutside(e) {
            if (activeColorPicker && !activeColorPicker.contains(e.target)) {
                hideColorPicker();
            }
        }
        
       // Click+drag the timecode text to scrub the value with live waveform feedback.
        // Horizontal drag: 1px = 10ms, Shift = 100ms (coarse), Alt = 1ms (fine).
        // A click without drag falls through to the prompt-based editor.
        let activeTimecodeScrub = null;
 
        function startTimecodeScrub(e, fieldId, markerIndex, which) {
            e.preventDefault();
            e.stopPropagation();
            const st = markerFieldsState[fieldId];
            if (!st) {
                // Field not loaded as a player marker (e.g., file isn't playing).
                // Fall back to prompt edit.
                editMarkerTime(fieldId, markerIndex, which);
                return;
            }
            const m = st.markers[markerIndex];
            if (!m) return;
            activeTimecodeScrub = {
                fieldId, markerIndex, which,
                startClientX: e.clientX,
                origStartSec: timeToSeconds(m.start),
                origEndSec: timeToSeconds(m.end),
                moved: false,
                spanEl: e.currentTarget,
            };
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        }
 
        document.addEventListener('mousemove', (e) => {
            if (!activeTimecodeScrub) return;
            const drag = activeTimecodeScrub;
            const dx = e.clientX - drag.startClientX;
            if (Math.abs(dx) > 2) drag.moved = true;
            // Sensitivity: 1px = 10ms by default. Shift coarse, Alt fine.
            let secPerPx = 0.01;
            if (e.shiftKey) secPerPx = 0.1;
            if (e.altKey)   secPerPx = 0.001;
            const dSec = dx * secPerPx;
 
            const st = markerFieldsState[drag.fieldId];
            if (!st) return;
            const m = st.markers[drag.markerIndex];
            if (!m) return;
            const dur = audioPlayer.duration || Infinity;
 
            if (drag.which === 'start') {
                let v = Math.max(0, Math.min(drag.origEndSec - 0.05, drag.origStartSec + dSec));
                m.start = secondsToHMS(v);
                if (drag.spanEl) drag.spanEl.textContent = m.start;
            } else {
                let v = Math.max(drag.origStartSec + 0.05, Math.min(dur, drag.origEndSec + dSec));
                m.end = secondsToHMS(v);
                if (drag.spanEl) drag.spanEl.textContent = m.end;
            }
            // Live update on the waveform
            updatePlayerMarkers();
        });
 
        document.addEventListener('mouseup', (e) => {
            if (!activeTimecodeScrub) return;
            const drag = activeTimecodeScrub;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            if (drag.moved) {
                const st = markerFieldsState[drag.fieldId];
                if (st) commitMarkerField(drag.fieldId, st.markers);
            } else {
                // Treat as a click → open the prompt editor
                editMarkerTime(drag.fieldId, drag.markerIndex, drag.which);
            }
            activeTimecodeScrub = null;
        });
 
        // Edit marker time
        function editMarkerTime(fieldId, markerIndex, which) {
            fetch('/api/file/' + selectedFileId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === fieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    const marker = markers[markerIndex];
                    if (!marker) return;
                    
                    const currentVal = which === 'start' ? marker.start : marker.end;
                    qdPrompt(`Edit ${which} time (HH:MM:SS or HH:MM:SS.mmm):`, currentVal, {title: 'Marker time', ok: 'Set', mono: true}).then(newVal => {
                    
                    if (newVal === null) return;
                    
                    // Validate time format — milliseconds (.mmm) optional
                    if (!/^\d{1,2}:\d{2}:\d{2}(\.\d{1,3})?$/.test(newVal)) {
                        alert('Invalid time format. Use HH:MM:SS or HH:MM:SS.mmm');
                        return;
                    }
                    
                    marker[which] = newVal;
                    
                    fetch('/api/file/' + selectedFileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        currentMarkers = markers;
                        if (markerFieldsState[fieldId]) markerFieldsState[fieldId].markers = markers;
                        updatePlayerMarkers();
                        loadFileMetadata(selectedFileId);
                    });
                    });
                });
        }
        
        // Edit marker label
        function editMarkerLabel(fieldId, markerIndex) {
            fetch('/api/file/' + selectedFileId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === fieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    const marker = markers[markerIndex];
                    if (!marker) return;
                    
                    qdPrompt('Marker label:', marker.label, {title: 'Edit marker', ok: 'Save'}).then(newLabel => {
                    
                    if (newLabel === null || newLabel.trim() === '') return;
                    
                    marker.label = newLabel.trim();
                    
                    fetch('/api/file/' + selectedFileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        currentMarkers = markers;
                        if (markerFieldsState[fieldId]) markerFieldsState[fieldId].markers = markers;
                        updatePlayerMarkers();
                        loadFileMetadata(selectedFileId);
                    });
                    });
                });
        }
        
        // Database management
        let currentDbPath = 'earmark.db';
        let recentDatabases = JSON.parse(localStorage.getItem('earmark_recent_dbs') || '[]');
        
        function updateDbDisplay() {
            document.getElementById('db-name-display').textContent = currentDbPath.split('/').pop();
            document.getElementById('db-name-display').title = currentDbPath;
        }
        
        function updateRecentDatabases() {
            const container = document.getElementById('recent-databases');
            if (recentDatabases.length === 0) {
                container.innerHTML = '<div class="dropdown-item" style="color: var(--grafite); font-style: italic;">No recent databases</div>';
            } else {
                container.innerHTML = recentDatabases.slice(0, 5).map(db => 
                    `<div class="dropdown-item" onclick="openDatabasePath('${db.replace(/'/g, "\\'")}')">${db.split('/').pop()}</div>`
                ).join('');
            }
        }
        
        function addToRecent(path) {
            recentDatabases = recentDatabases.filter(p => p !== path);
            recentDatabases.unshift(path);
            recentDatabases = recentDatabases.slice(0, 10);
            localStorage.setItem('earmark_recent_dbs', JSON.stringify(recentDatabases));
            updateRecentDatabases();
        }
        
        function toggleDropdown(id) {
            const menu = document.getElementById(id);
            const wasShown = menu.classList.contains('show');
            
            // Close all dropdowns
            document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
            
            if (!wasShown) {
                menu.classList.add('show');
                updateRecentDatabases();
            }
        }
        
        // Close dropdown when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.dropdown')) {
                document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
            }
        });
        
        function newDatabase() {
            const ask = nativeDialogs
                ? pickPath('savedb', 'new_project.db').then(p => p ? p[0] : null)
                : qdPrompt('Name of the new database. It is created in your Earmark folder (Documents/Earmark).', 'new_project.db', {title: 'New database', ok: 'Create', mono: true});
            ask.then(name => {
            if (!name) return;
            
            const dbName = name.endsWith('.db') ? name : name + '.db';
            
            fetch('/api/database/new', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: dbName})
            }).then(r => r.json())
            .then(data => {
                if (data.success) {
                    currentDbPath = data.path;
                    addToRecent(data.path);
                    updateDbDisplay();
                    
                    // Clear current state
                    selectedFileId = null;
                    currentPlayingId = null;
                    selectedFileIds.clear();
                    currentMarkers = [];
                    currentMarkersFieldId = null;
                    markerFieldsState = {};
                    undoStack = [];
                    redoStack = [];
                    
                    // Hide player bar
                    document.getElementById('player-bar').classList.remove('visible');
                    document.getElementById('wave-strip').classList.remove('has-audio');
                    document.getElementById('selected-file-name').textContent = 'No file selected';
                    document.getElementById('metadata-panel').innerHTML = '<div class="empty-state">Select a file to edit metadata</div>';
                    
                    loadFields();
                    loadFiles();
                    document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
                    qdNotice('Created new database: ' + dbName);
                } else {
                    alert('Error: ' + data.error);
                }
            });
            });
        }
        
        function openDatabase() {
            document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
            const ask = nativeDialogs
                ? pickPath('db').then(p => p ? p[0] : null)
                : qdPrompt('Database file: a full path, or just a name for one in Documents/Earmark.', '', {title: 'Open database', ok: 'Open', mono: true, placeholder: 'archive.db  or  /Users/…/archive.db'});
            ask.then(path => {
            if (!path) return;
            openDatabasePath(path);
            });
        }
        
        function openDatabasePath(path) {
            fetch('/api/database/open', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path})
            }).then(r => r.json())
            .then(data => {
                if (data.success) {
                    currentDbPath = data.path;
                    addToRecent(data.path);
                    updateDbDisplay();
                    
                    // Clear current state
                    selectedFileId = null;
                    currentPlayingId = null;
                    selectedFileIds.clear();
                    currentMarkers = [];
                    currentMarkersFieldId = null;
                    markerFieldsState = {};
                    undoStack = [];
                    redoStack = [];
                    
                    // Hide player bar
                    document.getElementById('player-bar').classList.remove('visible');
                    document.getElementById('wave-strip').classList.remove('has-audio');
                    document.getElementById('selected-file-name').textContent = 'No file selected';
                    document.getElementById('metadata-panel').innerHTML = '<div class="empty-state">Select a file to edit metadata</div>';
                    
                    loadFields();
                    loadFiles();
                    document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
                } else {
                    alert('Error: ' + data.error);
                }
            });
        }
        
        function saveDatabaseAs() {
            const ask = nativeDialogs
                ? pickPath('savedb', currentDbPath.split('/').pop()).then(p => p ? p[0] : null)
                : qdPrompt('Save a copy of this database as:', currentDbPath.split('/').pop(), {title: 'Save database as', ok: 'Save', mono: true});
            ask.then(name => {
            if (!name) return;
            
            const dbName = name.endsWith('.db') ? name : name + '.db';
            
            fetch('/api/database/saveas', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: dbName})
            }).then(r => r.json())
            .then(data => {
                if (data.success) {
                    currentDbPath = data.path;
                    addToRecent(data.path);
                    updateDbDisplay();
                    qdNotice('Saved as: ' + dbName);
                } else {
                    alert('Error: ' + data.error);
                }
            });
            });
        }
        
        // Exports are written next to the current database and shown in Finder
        // (the desktop window can't do browser-style downloads).
        function saveExport(kind, what) {
            document.querySelectorAll('.dropdown-menu').forEach(function (m) { m.classList.remove('show'); });
            var req = kind === 'xlsx'
                ? fetch('/api/export/xlsx/save', { method: 'POST' })
                : fetch('/api/export/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind: kind}) });
            req.then(function (r) { return r.json(); })
                .then(function (res) {
                    if (res.success) qdNotice(what + ' saved:\\n' + res.path);
                    else alert('Could not save the ' + what.toLowerCase() + ': ' + (res.error || 'unknown error'));
                })
                .catch(function (err) { alert('Could not save the ' + what.toLowerCase() + ': ' + err.message); });
        }
        function exportToCSV() { saveExport('csv', 'CSV export'); }

        function exportToExcel() { saveExport('xlsx', 'Excel export'); }

        // Playback speed
        function changePlaybackSpeed(speed) {
            audioPlayer.playbackRate = parseFloat(speed);
        }
        
        // Initialize
        updateRecentDatabases();
        fetch('/api/database/current').then(r => r.json()).then(data => {
            if (data.path) {
                currentDbPath = data.path;
                updateDbDisplay();
            }
        });
        
        // Theme toggle
        function toggleTheme() {
            const html = document.documentElement;
            const current = html.getAttribute('data-theme');
            const newTheme = current === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-theme', newTheme);
            // Button label shows the *opposite* mode (the one a click would switch to).
            document.getElementById('theme-btn').textContent = newTheme === 'light' ? 'Dark' : 'Light';
            localStorage.setItem('earmark-theme', newTheme);
            
            // Redraw waveform with new colors
            if (currentWaveformPeaks) {
                drawWaveform(currentWaveformPeaks);
            }
        }
        
        // Restore saved theme
        (function() {
            var saved = null;
            try { saved = localStorage.getItem('earmark-theme'); } catch (e) {}
            if (saved === 'dark') {
                document.documentElement.setAttribute('data-theme', 'dark');
                document.getElementById('theme-btn').textContent = 'Light';
            } else {
                document.documentElement.setAttribute('data-theme', 'light');
                document.getElementById('theme-btn').textContent = 'Dark';
            }
        })();
        
        // Waveform display
        // currentWaveformPeaks is kept as a truthy "we have data" flag for the many
        // existing `if (currentWaveformPeaks) drawWaveform(...)` call sites.
        let currentWaveformPeaks = null;
        var currentWaveformData = null;        // last /waveform response (mode, channels|samples, window)
        let currentWaveformWindow = [0, 1];    // [startFrac, endFrac] of audio that the data covers
        let currentWaveformFileId = null;
        var currentWaveformMeta = null;        // {frames, sampleRate, nChannels, duration}
        let waveformFetchSeq = 0;              // monotonic counter to ignore stale responses
        var waveformLastCanvasW = 0;           // to detect width changes -> refetch at new resolution
        var WAVEFORM_DB_FLOOR = -60;           // dB at the centre line in dB mode
        var waveformDbMode = false;
        try { waveformDbMode = localStorage.getItem('earmark_wave_db') === '1'; } catch (e) {}

        function waveformCanvasPxWidth() {
            var canvas = document.getElementById('waveform-canvas');
            if (!canvas) return 1000;
            var dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
            return Math.max(100, Math.round(canvas.parentElement.offsetWidth * dpr));
        }

        function loadWaveform(fileId) {
            var canvas = document.getElementById('waveform-canvas');
            if (!canvas) return;

            // Reset zoom & state whenever a new file's waveform is loaded
            waveformZoom = 1.0;
            waveformOffset = 0.0;
            currentWaveformFileId = fileId;
            currentWaveformWindow = [0, 1];
            currentWaveformData = null;
            currentWaveformPeaks = null;
            currentWaveformMeta = null;
            if (typeof clearWaveSelection === 'function') clearWaveSelection();
            fetchWaveformWindow(0, 1);
        }

        // Fetch waveform data for a specific [startFrac, endFrac] window, at a
        // resolution matched to the canvas (≈ one column per device pixel).
        // Each call increments waveformFetchSeq so out-of-order responses are dropped.
        function fetchWaveformWindow(startFrac, endFrac) {
            if (!currentWaveformFileId) return;
            var seq = ++waveformFetchSeq;
            var viewSpan = 1 / waveformZoom;
            var bars = Math.round(waveformCanvasPxWidth() * (endFrac - startFrac) / viewSpan);
            bars = Math.max(64, Math.min(8000, bars));
            var url = '/api/audio/' + currentWaveformFileId +
                      '/waveform?bars=' + bars +
                      '&start=' + String(startFrac) +
                      '&end=' + String(endFrac);
            fetch(url)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (seq !== waveformFetchSeq) return; // a newer fetch already started
                    if (!data || data.error || (!data.channels && !data.samples)) {
                        if (data && data.error) console.log('Waveform:', data.error);
                        return;
                    }
                    currentWaveformData = data;
                    currentWaveformPeaks = data;
                    currentWaveformWindow = data.window || [startFrac, endFrac];
                    if (data.frames) {
                        currentWaveformMeta = {
                            frames: data.frames, sampleRate: data.sampleRate,
                            nChannels: data.nChannels, duration: data.duration
                        };
                        // Allow zooming until ~8 screen px per sample.
                        var cssW = document.getElementById('player-progress-container').offsetWidth || 800;
                        MAX_WAVEFORM_ZOOM = Math.max(64, data.frames / Math.max(16, cssW / 8));
                    }
                    drawWaveform(currentWaveformPeaks);
                    updatePlayerMarkers();
                    updatePlayheadPosition();
                })
                .catch(function (e) {
                    if (seq !== waveformFetchSeq) return;
                    console.log('Waveform load failed:', e);
                });
        }

        // Debounced refetch — called from zoom/pan after the user stops moving briefly.
        // force=true refetches the visible window even if the current data covers it
        // (used after the canvas changes width).
        let waveformRefetchTimer = null;
        function scheduleWaveformRefetch(force) {
            if (waveformRefetchTimer) clearTimeout(waveformRefetchTimer);
            waveformRefetchTimer = setTimeout(function () {
                var viewStart = waveformOffset;
                var viewEnd   = waveformOffset + 1 / waveformZoom;
                var winStart = currentWaveformWindow[0], winEnd = currentWaveformWindow[1];
                var winSpan = winEnd - winStart;
                var viewSpan = viewEnd - viewStart;
                var outOfBounds = (viewStart < winStart - 1e-12) || (viewEnd > winEnd + 1e-12);
                // Viewing less than half of what we fetched -> fetch sharper data.
                // (Not needed if we already hold the raw samples for this window.)
                var tooCoarse = (viewSpan < winSpan * 0.5) &&
                                !(currentWaveformData && currentWaveformData.mode === 'samples');
                if (force || outOfBounds || tooCoarse) {
                    // Fetch a window slightly larger than the visible area so small pans
                    // don't trigger another fetch.
                    var pad = viewSpan * 0.25;
                    fetchWaveformWindow(Math.max(0, viewStart - pad), Math.min(1, viewEnd + pad));
                }
            }, 120);
        }

        function toggleWaveformDb() {
            waveformDbMode = !waveformDbMode;
            try { localStorage.setItem('earmark_wave_db', waveformDbMode ? '1' : '0'); } catch (e) {}
            updateWaveformDbButton();
            if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
        }

        function updateWaveformDbButton() {
            var b = document.getElementById('wave-db-btn');
            if (!b) return;
            b.textContent = waveformDbMode ? 'dB' : 'Lin';
            b.title = waveformDbMode
                ? 'Amplitude scale: dB (log). Click for linear.'
                : 'Amplitude scale: linear. Click for dB (log).';
            b.className = 'zoom-btn' + (waveformDbMode ? ' active' : '');
        }

        // Map a signed amplitude (-1..1, already gain-scaled) to a signed
        // display value (-1..1) in linear or dB scale.
        function waveMapAmp(a) {
            if (!waveformDbMode) return a;
            var s = a < 0 ? -1 : 1;
            var m = Math.abs(a);
            if (m < 1e-7) return 0;
            var n = (20 * Math.log(m) / Math.LN10 - WAVEFORM_DB_FLOOR) / (-WAVEFORM_DB_FLOOR);
            if (n < 0) n = 0;
            return s * n;
        }

        function drawWaveform(peaks) {
            var canvas = document.getElementById('waveform-canvas');
            if (!canvas) return;

            var container = canvas.parentElement;
            var dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
            var lane = displayLanes(container);
            var W = Math.max(1, Math.round(container.offsetWidth * dpr));
            var H = Math.max(1, Math.round(lane.waveH * dpr));
            canvas.width = W;
            canvas.height = H;
            canvas.style.width = container.offsetWidth + 'px';
            canvas.style.height = lane.waveH + 'px';
            canvas.style.top = lane.waveTop + 'px';

            // If the canvas changed width, fetch data at the new resolution.
            if (waveformLastCanvasW && Math.abs(W - waveformLastCanvasW) > 4 && currentWaveformData) {
                scheduleWaveformRefetch(true);
            }
            waveformLastCanvasW = W;

            var ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, W, H);

            var isLight = document.documentElement.getAttribute('data-theme') === 'light';
            // Colours come from the theme's CSS variables (ui/quadro.css),
            // with the old values as fallbacks if the stylesheet is missing.
            var themeCss = getComputedStyle(document.documentElement);
            function themeColor(name, fallback) {
                var v = themeCss.getPropertyValue(name).trim();
                return v || fallback;
            }
            var peakColor  = themeColor('--wave-peak',   isLight ? 'rgba(30, 41, 59, 0.40)' : 'rgba(148, 197, 255, 0.50)');
            var rmsColor   = themeColor('--wave-rms',    isLight ? 'rgba(15, 23, 42, 0.80)' : 'rgba(214, 234, 255, 0.90)');
            var lineColor  = themeColor('--wave-line',   isLight ? 'rgba(15, 23, 42, 0.90)' : 'rgba(186, 220, 255, 0.95)');
            var centerLine = themeColor('--wave-center', isLight ? 'rgba(0,0,0,0.15)' : 'rgba(255,255,255,0.12)');
            var gridLine   = themeColor('--wave-grid',   isLight ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.05)');
            var laneSep    = themeColor('--wave-lane',   isLight ? 'rgba(0,0,0,0.20)' : 'rgba(0,0,0,0.45)');
            var labelColor = themeColor('--wave-label',  isLight ? 'rgba(15,23,42,0.55)' : 'rgba(226,232,240,0.45)');
            var clipColor  = themeColor('--wave-clip',   '#ef4444');

            clampWaveformOffset();

            var data = currentWaveformData;
            var nCh = 1;
            if (data && data.mode === 'samples' && data.samples) nCh = data.samples.length;
            else if (data && data.channels) nCh = data.channels.length;
            nCh = Math.max(1, nCh);

            var viewStart = waveformOffset;
            var viewSpan  = 1 / waveformZoom;
            var winStart = currentWaveformWindow[0];
            var winSpan  = (currentWaveformWindow[1] - currentWaveformWindow[0]) || 1;
            var gain = waveformVScale;
            var laneH = H / nCh;

            // Lane furniture: centre lines, dB grid, separators, channel labels
            var chNames = nCh === 2 ? ['L', 'R'] : null;
            for (var c = 0; c < nCh; c++) {
                var top = c * laneH;
                var mid = top + laneH / 2;
                var half = (laneH / 2) * 0.92;
                if (waveformDbMode) {
                    ctx.fillStyle = gridLine;
                    var grid = [-6, -12, -24, -48];
                    for (var g = 0; g < grid.length; g++) {
                        var gn = (grid[g] - WAVEFORM_DB_FLOOR) / (-WAVEFORM_DB_FLOOR);
                        ctx.fillRect(0, Math.round(mid - gn * half), W, 1);
                        ctx.fillRect(0, Math.round(mid + gn * half), W, 1);
                    }
                }
                ctx.fillStyle = centerLine;
                ctx.fillRect(0, Math.round(mid), W, 1);
                if (c > 0) {
                    ctx.fillStyle = laneSep;
                    ctx.fillRect(0, Math.round(top), W, Math.max(1, Math.round(dpr)));
                }
                if (nCh > 1 && laneH > 24 * dpr) {
                    ctx.fillStyle = labelColor;
                    ctx.font = (9 * dpr) + 'px -apple-system, sans-serif';
                    ctx.fillText(chNames ? chNames[c] : String(c + 1), 4 * dpr, top + 11 * dpr);
                }
            }

            function clampY(y, top) { return Math.max(top, Math.min(top + laneH, y)); }

            if (data && data.mode === 'samples' && data.samples) {
                // ---- Sample level: polyline through the actual samples (+ dots when roomy)
                for (var c2 = 0; c2 < nCh; c2++) {
                    var s = data.samples[c2];
                    if (!s || !s.length) continue;
                    var top2 = c2 * laneH, mid2 = top2 + laneH / 2, half2 = (laneH / 2) * 0.92;
                    var N = s.length;
                    var pxPerSample = (winSpan / N) / viewSpan * W;
                    var i0 = Math.max(0, Math.floor(((viewStart - winStart) / winSpan) * N) - 1);
                    var i1 = Math.min(N, Math.ceil(((viewStart + viewSpan - winStart) / winSpan) * N) + 1);
                    ctx.strokeStyle = lineColor;
                    ctx.lineWidth = Math.max(1, dpr);
                    ctx.beginPath();
                    for (var i = i0; i < i1; i++) {
                        var f = winStart + (i / N) * winSpan;
                        var x = (f - viewStart) / viewSpan * W;
                        var y = clampY(mid2 - waveMapAmp(s[i] * gain) * half2, top2);
                        if (i === i0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                    }
                    ctx.stroke();
                    if (pxPerSample >= 6 * dpr) {
                        ctx.fillStyle = lineColor;
                        var r = Math.min(3 * dpr, pxPerSample / 4);
                        for (var j = i0; j < i1; j++) {
                            var fj = winStart + (j / N) * winSpan;
                            var xj = (fj - viewStart) / viewSpan * W;
                            var yj = clampY(mid2 - waveMapAmp(s[j] * gain) * half2, top2);
                            ctx.beginPath();
                            ctx.arc(xj, yj, r, 0, Math.PI * 2);
                            ctx.fill();
                        }
                    }
                }
            } else if (data && data.channels) {
                // ---- Envelope: one column per device pixel, peak (min/max) + RMS body
                for (var c3 = 0; c3 < nCh; c3++) {
                    var ch = data.channels[c3];
                    if (!ch || !ch.max || !ch.max.length) continue;
                    var top3 = c3 * laneH, mid3 = top3 + laneH / 2, half3 = (laneH / 2) * 0.92;
                    var mins = ch.min, maxs = ch.max, rmss = ch.rms;
                    var M = maxs.length;
                    var colTop = new Array(W), colBot = new Array(W);
                    var rTop = rmss ? new Array(W) : null, rBot = rmss ? new Array(W) : null;
                    var clipT = [], clipB = [];
                    for (var px = 0; px < W; px++) {
                        var fa = viewStart + (px / W) * viewSpan;
                        var fb = viewStart + ((px + 1) / W) * viewSpan;
                        var ia = (fa - winStart) / winSpan * M;
                        var ib = (fb - winStart) / winSpan * M;
                        if (ib <= 0 || ia >= M) { colTop[px] = null; continue; }
                        var a = Math.max(0, Math.floor(ia));
                        var b = Math.min(M, Math.ceil(ib));
                        if (b <= a) b = Math.min(M, a + 1);
                        var mn = 1e9, mx = -1e9, ss = 0, cnt = 0;
                        for (var k = a; k < b; k++) {
                            if (mins[k] < mn) mn = mins[k];
                            if (maxs[k] > mx) mx = maxs[k];
                            if (rmss) { ss += rmss[k] * rmss[k]; cnt++; }
                        }
                        if (mx >= 0.999) clipT.push(px);
                        if (mn <= -0.999) clipB.push(px);
                        var yT = clampY(mid3 - waveMapAmp(mx * gain) * half3, top3);
                        var yB = clampY(mid3 - waveMapAmp(mn * gain) * half3, top3);
                        colTop[px] = yT; colBot[px] = yB;
                        if (rmss && cnt) {
                            var rv = Math.sqrt(ss / cnt);
                            var rT = clampY(mid3 - waveMapAmp(rv * gain) * half3, top3);
                            var rB = clampY(mid3 + waveMapAmp(rv * gain) * half3, top3);
                            // keep the RMS body inside the peak envelope
                            rTop[px] = Math.max(rT, yT);
                            rBot[px] = Math.min(rB, yB);
                        }
                    }
                    ctx.fillStyle = peakColor;
                    for (var p = 0; p < W; p++) {
                        if (colTop[p] == null) continue;
                        ctx.fillRect(p, colTop[p], 1, Math.max(1, colBot[p] - colTop[p]));
                    }
                    if (rTop) {
                        ctx.fillStyle = rmsColor;
                        for (var q = 0; q < W; q++) {
                            if (colTop[q] == null || rTop[q] == null) continue;
                            if (rBot[q] - rTop[q] > 0) ctx.fillRect(q, rTop[q], 1, rBot[q] - rTop[q]);
                        }
                    }
                    if (clipT.length || clipB.length) {
                        ctx.fillStyle = clipColor;
                        var ch2 = Math.max(2, Math.round(2 * dpr));
                        for (var u = 0; u < clipT.length; u++) ctx.fillRect(clipT[u], top3, 1, ch2);
                        for (var v = 0; v < clipB.length; v++) ctx.fillRect(clipB[v], top3 + laneH - ch2, 1, ch2);
                    }
                }
            }

            // Update the zoom-level display
            var lvl = document.getElementById('zoom-level-display');
            if (lvl) {
                var zt;
                if (waveformZoom >= 10000) zt = Math.round(waveformZoom / 1000) + 'k';
                else if (waveformZoom >= 1000) zt = (waveformZoom / 1000).toFixed(1) + 'k';
                else zt = waveformZoom.toFixed(waveformZoom >= 10 ? 0 : 1);
                lvl.textContent = zt + 'x';
                if (currentWaveformMeta && audioPlayer && audioPlayer.duration) {
                    var visSec = audioPlayer.duration / waveformZoom;
                    lvl.title = 'Visible: ' + (visSec >= 1 ? visSec.toFixed(2) + ' s' : (visSec * 1000).toFixed(1) + ' ms') +
                        ' (' + Math.round(currentWaveformMeta.frames / waveformZoom) + ' samples)';
                }
            }
            var outBtn = document.getElementById('zoom-out-btn');
            var inBtn  = document.getElementById('zoom-in-btn');
            if (outBtn) outBtn.disabled = (waveformZoom <= MIN_WAVEFORM_ZOOM + 0.001);
            if (inBtn)  inBtn.disabled  = (waveformZoom >= MAX_WAVEFORM_ZOOM - 0.001);
        }

        function clampWaveformOffset() {
            if (waveformZoom <= 1) { waveformOffset = 0; return; }
            const maxOffset = 1 - 1/waveformZoom;
            if (waveformOffset < 0) waveformOffset = 0;
            if (waveformOffset > maxOffset) waveformOffset = maxOffset;
        }

        // Zoom in/out around the playhead (or center if no playback)
        function zoomWaveform(factor) {
            const newZoom = Math.max(MIN_WAVEFORM_ZOOM, Math.min(MAX_WAVEFORM_ZOOM, waveformZoom * factor));
            if (newZoom === waveformZoom) return;

            // Anchor: keep the playhead (or visible-center) at the same screen position
            let anchorFrac;
            if (audioPlayer && audioPlayer.duration) {
                anchorFrac = audioPlayer.currentTime / audioPlayer.duration;
            } else {
                anchorFrac = waveformOffset + 0.5/waveformZoom;
            }
            // Screen-x fraction of anchor within current view
            let screenFrac = (anchorFrac - waveformOffset) * waveformZoom;
            if (screenFrac < 0 || screenFrac > 1) screenFrac = 0.5;

 waveformZoom = newZoom;
            waveformOffset = anchorFrac - screenFrac / waveformZoom;
            clampWaveformOffset();
 
            if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
            updatePlayerMarkers();
            updatePlayheadPosition();
            scheduleWaveformRefetch();
        }
 
        function resetWaveformZoom() {
            waveformZoom = 1.0;
            waveformOffset = 0.0;
            waveformVScale = 1.0;
            if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
            updatePlayerMarkers();
            updatePlayheadPosition();
            scheduleWaveformRefetch();
        }
 
        // Shift the visible window by the given fraction of the visible window width
        function panWaveform(deltaFraction) {
            if (waveformZoom <= 1) return;
            waveformOffset += deltaFraction / waveformZoom;
            clampWaveformOffset();
            if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
            updatePlayerMarkers();
            updatePlayheadPosition();
            scheduleWaveformRefetch();
        }
 
        // Keep playhead visible during playback (auto-scroll when close to edge)
        function followPlayheadIfNeeded() {
            if (waveformZoom <= 1 || !audioPlayer || !audioPlayer.duration) return;
            const playFrac = audioPlayer.currentTime / audioPlayer.duration;
            const screenFrac = (playFrac - waveformOffset) * waveformZoom;
            if (screenFrac > 0.9 || screenFrac < 0.0) {
                // Jump so the playhead lands at 10% of the view
                waveformOffset = playFrac - 0.1/waveformZoom;
                clampWaveformOffset();
                if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
                updatePlayerMarkers();
                scheduleWaveformRefetch();
            }
        }

        // Redraw waveform on window resize
        window.addEventListener('resize', () => {
            if (currentWaveformPeaks) {
                drawWaveform(currentWaveformPeaks);
                updatePlayerMarkers();
                updatePlayheadPosition();
            }
        });
        
        // Audio file info
        function loadAudioInfo(fileId) {
            fetch('/api/audio/' + fileId + '/info')
                .then(r => r.json())
                .then(info => {
                    const parts = [];
                    if (info.format) parts.push(info.format);
                    if (info.sampleRate) parts.push(info.sampleRate / 1000 + 'kHz');
                    if (info.bitDepth) parts.push(info.bitDepth + 'bit' + (info.float ? ' float' : ''));
                    if (info.channels) parts.push(info.channels === 1 ? 'Mono' : info.channels === 2 ? 'Stereo' : info.channels + 'ch');
                    if (info.filesize) parts.push(formatFileSize(info.filesize));
                    
                    document.getElementById('audio-info').textContent = parts.join(' · ');
                })
                .catch(e => {
                    document.getElementById('audio-info').textContent = '';
                });
        }
        
        function formatFileSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }
        
        // Drag and Drop import
        let dragCounter = 0;
        
        document.addEventListener('dragenter', function(e) {
            e.preventDefault();
            dragCounter++;
            document.getElementById('drop-overlay').classList.add('active');
        });
        
        document.addEventListener('dragleave', function(e) {
            e.preventDefault();
            dragCounter--;
            if (dragCounter === 0) {
                document.getElementById('drop-overlay').classList.remove('active');
            }
        });
        
        document.addEventListener('dragover', function(e) {
            e.preventDefault();
        });
        
        document.addEventListener('drop', function(e) {
            e.preventDefault();
            dragCounter = 0;
            document.getElementById('drop-overlay').classList.remove('active');
            
            // Get the dropped items
            const items = e.dataTransfer.items;
            if (!items || items.length === 0) return;
            
            // Try to get folder path from dropped items
            // Note: browsers have limited folder access for security reasons
            // The dropped files will have webkitRelativePath or we handle files directly
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                // Collect unique folder paths from dropped files
                const paths = new Set();
                let hasFolder = false;
                
                for (let i = 0; i < files.length; i++) {
                    const file = files[i];
                    // If the file has a path attribute (Electron/NWjs), use it
                    if (file.path) {
                        // Get parent directory
                        const lastSlash = Math.max(file.path.lastIndexOf('/'), file.path.lastIndexOf('\\\\'));
                        if (lastSlash > 0) {
                            paths.add(file.path.substring(0, lastSlash));
                            hasFolder = true;
                        }
                    }
                }
                
                if (hasFolder) {
                    const folderPath = Array.from(paths)[0];
                    const ask = paths.size > 1 ? Promise.resolve(true)
                        : qdConfirm('Also import audio from subfolders of ' + folderPath + '?', {title: 'Import audio', ok: 'Include subfolders', cancel: 'This folder only'});
                    ask.then(recursive => {
                        fetch('/api/import', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({path: folderPath, recursive})
                        }).then(r => r.json()).then(result => {
                            qdNotice('Imported ' + result.imported + ' file(s)');
                            loadFiles();
                        });
                    });
                } else {
                    // Fallback: ask for path (browser security limits direct folder access)
                    alert('Browser security prevents reading folder paths from drag-and-drop.\\nPlease use the Import Audio button instead and enter the folder path.');
                }
            }
        });
        
        // Load initial data
        loadFields();
        loadFiles();
        
        // Keyboard shortcuts
        // ---- Quadro dialogs: message, confirm, prompt and small forms ----
        // qdDialog({title, message, fields, ok, cancel, danger, validate}) -> Promise
        //   resolves to {name: value} (or true when there are no fields), or null if cancelled.
        var qdQueue = [];
        var qdOpen = false;
        function qdDialog(opts) {
            return new Promise(function (resolve) {
                qdQueue.push({ opts: opts || {}, resolve: resolve });
                if (!qdOpen) qdNext();
            });
        }
        function qdNext() {
            var job = qdQueue.shift();
            if (!job) { qdOpen = false; return; }
            qdOpen = true;
            var o = job.opts, fields = o.fields || [];
            var prevFocus = document.activeElement;
            var overlay = document.createElement('div');
            overlay.className = 'modal-overlay visible qd-generic';
            var box = document.createElement('div');
            box.className = 'modal qd-dialog';
            box.setAttribute('role', o.cancel === null ? 'alertdialog' : 'dialog');
            var bar = document.createElement('div');
            bar.className = 'qd-dialog-bar';
            var title = document.createElement('span');
            title.textContent = o.title || 'Earmark';
            bar.appendChild(title);
            var body = document.createElement('div');
            body.className = 'qd-dialog-body';
            if (o.message) {
                var msg = document.createElement('div');
                msg.className = 'qd-message';
                msg.textContent = o.message;
                body.appendChild(msg);
            }
            var inputs = {};
            var err = document.createElement('div');
            err.className = 'nf-error';
            fields.forEach(function (f, idx) {
                var id = 'qd-f-' + idx;
                if (f.type === 'check') {
                    var row = document.createElement('label');
                    row.className = 'qd-check';
                    var cb = document.createElement('input');
                    cb.type = 'checkbox'; cb.id = id; cb.checked = !!f.value;
                    var t = document.createElement('span');
                    t.textContent = f.label;
                    row.appendChild(cb); row.appendChild(t);
                    body.appendChild(row);
                    inputs[f.name] = cb;
                    return;
                }
                var lab = document.createElement('label');
                lab.className = 'qd-label';
                lab.setAttribute('for', id);
                lab.textContent = f.label || '';
                if (f.label) body.appendChild(lab);
                var el;
                if (f.type === 'select') {
                    el = document.createElement('select');
                    (f.options || []).forEach(function (opt) {
                        var oe = document.createElement('option');
                        oe.value = opt.value; oe.textContent = opt.label;
                        el.appendChild(oe);
                    });
                    if (f.value != null) el.value = f.value;
                } else {
                    el = document.createElement('input');
                    el.type = f.type === 'number' ? 'number' : 'text';
                    el.value = f.value != null ? f.value : '';
                    el.autocomplete = 'off';
                    if (f.placeholder) el.placeholder = f.placeholder;
                    if (f.mono) el.className = 'qd-mono';
                }
                el.id = id;
                inputs[f.name] = el;
                if (f.browse && nativeDialogs && el.tagName === 'INPUT') {
                    // [ path box ][ Folder… ][ Files… ]
                    var row = document.createElement('div');
                    row.className = 'qd-pathrow';
                    row.appendChild(el);
                    el.addEventListener('input', function () { this._paths = null; });
                    [].concat(f.browse).forEach(function (b) {
                        var key = document.createElement('button');
                        key.type = 'button';
                        key.className = 'btn btn-secondary qd-browse';
                        key.textContent = b.label;
                        key.addEventListener('click', function () {
                            pickPath(b.kind).then(function (paths) {
                                if (!paths) return;
                                if (b.kind === 'files') {
                                    el._paths = paths;
                                    el.value = paths.length === 1 ? paths[0]
                                        : paths.length + ' files: ' + paths.slice(0, 3).map(baseName).join(', ') + (paths.length > 3 ? ' …' : '');
                                } else {
                                    el._paths = null;
                                    el.value = paths[0];
                                }
                                if (typeof f.onPick === 'function') f.onPick(b.kind, paths, inputs);
                                err.style.display = 'none';
                            });
                        });
                        row.appendChild(key);
                    });
                    body.appendChild(row);
                } else {
                    body.appendChild(el);
                }
            });
            body.appendChild(err);
            var keys = document.createElement('div');
            keys.className = 'qd-dialog-keys';
            var cancelBtn = null;
            if (o.cancel !== null) {
                cancelBtn = document.createElement('button');
                cancelBtn.className = 'btn btn-secondary';
                cancelBtn.textContent = o.cancel || 'Cancel';
                keys.appendChild(cancelBtn);
            }
            var okBtn = document.createElement('button');
            okBtn.className = 'btn' + (o.danger ? ' qd-commit' : ' qd-ok');
            okBtn.textContent = o.ok || 'OK';
            keys.appendChild(okBtn);
            box.appendChild(bar); box.appendChild(body); box.appendChild(keys);
            overlay.appendChild(box);
            document.body.appendChild(overlay);

            function values() {
                var v = {};
                fields.forEach(function (f) {
                    var el = inputs[f.name];
                    v[f.name] = f.type === 'check' ? el.checked : el.value;
                    if (el._paths) v[f.name + 'Paths'] = el._paths;
                });
                return v;
            }
            function finish(result) {
                overlay.parentNode && overlay.parentNode.removeChild(overlay);
                if (prevFocus && prevFocus.focus && prevFocus.isConnected) { try { prevFocus.focus(); } catch (e) {} }
                job.resolve(result);
                qdNext();
            }
            function confirmIt() {
                var v = values();
                if (o.validate) {
                    var problem = o.validate(v);
                    if (problem) { err.textContent = problem; err.style.display = 'block'; return; }
                }
                finish(fields.length ? v : true);
            }
            function cancelIt() { finish(o.cancel === null ? true : null); }
            body.addEventListener('input', function () { err.style.display = 'none'; });
            okBtn.addEventListener('click', confirmIt);
            if (cancelBtn) cancelBtn.addEventListener('click', cancelIt);
            overlay.addEventListener('mousedown', function (e) { if (e.target === overlay && o.cancel !== null) cancelIt(); });
            overlay.addEventListener('keydown', function (e) {
                if (e.key === 'Escape') { e.preventDefault(); cancelIt(); }
                else if (e.key === 'Enter' && !(e.target && e.target.classList && e.target.classList.contains('btn-secondary'))) { e.preventDefault(); confirmIt(); }
                e.stopPropagation();                      // no app shortcuts while a dialog is open
            });
            var first = null;
            for (var k in inputs) { if (inputs[k].type !== 'checkbox') { first = inputs[k]; break; } }
            setTimeout(function () {
                if (first && first.tagName === 'INPUT') { first.focus(); first.select(); }
                else if (first && first._qsel) first._qsel.btn.focus();
                else okBtn.focus();
            }, 0);
        }
        // Native Finder / Explorer dialogs, when Earmark runs in its own window.
        var nativeDialogs = false;
        fetch('/api/dialog').then(function (r) { return r.json(); })
            .then(function (d) { nativeDialogs = !!(d && d.available); }).catch(function () {});
        function pickPath(kind, filename) {
            return fetch('/api/dialog', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({kind: kind, filename: filename || ''})
            }).then(function (r) { return r.json(); })
              .then(function (d) {
                  if (d && d.error) { alert('Could not open the file dialog: ' + d.error); return null; }
                  return (d && d.paths && d.paths.length) ? d.paths : null;
              })
              .catch(function () { return null; });
        }
        function baseName(p) { return String(p).split(/[\\\\/]/).pop(); }

        function qdAlert(message, opts) {
            opts = opts || {};
            return qdDialog({ title: opts.title || 'Earmark', message: String(message), ok: 'OK', cancel: null });
        }
        function qdConfirm(message, opts) {
            opts = opts || {};
            return qdDialog({ title: opts.title || 'Earmark', message: message, ok: opts.ok || 'OK', cancel: opts.cancel || 'Cancel', danger: !!opts.danger })
                .then(function (r) { return r !== null; });
        }
        function qdPrompt(message, def, opts) {
            opts = opts || {};
            return qdDialog({ title: opts.title || 'Earmark', message: message, ok: opts.ok || 'OK',
                              fields: [{ name: 'v', type: 'text', value: def == null ? '' : def, mono: !!opts.mono, placeholder: opts.placeholder }] })
                .then(function (r) { return r ? r.v : null; });
        }
        // A short notice in the corner that fades by itself (successes, "Imported 12 files").
        function qdNotice(message) {
            var host = document.getElementById('qd-notices');
            if (!host) {
                host = document.createElement('div');
                host.id = 'qd-notices';
                document.body.appendChild(host);
            }
            var n = document.createElement('div');
            n.className = 'qd-notice';
            n.textContent = String(message);
            host.appendChild(n);
            setTimeout(function () { n.classList.add('is-leaving'); }, 3200);
            setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); }, 3700);
        }
        // Every remaining alert() in the app now opens a Quadro message instead.
        window.alert = function (message) { qdAlert(message); };

        // ---- Keyboard shortcuts: one table of actions, customisable in the ? window ----
        // Combos are written as e.g. 'Mod+Shift+K' (Mod = Cmd on Mac, Ctrl elsewhere),
        // 'Space', 'Down', 'F7', 'Mod+Shift+Comma'.
        var IS_MAC = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
        function isTypingTarget() {
            var el = document.activeElement;
            if (!el) return false;
            var t = el.tagName;
            return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || el.isContentEditable;
        }
        function playOrPause() {
            if (currentPlayingId) {
                togglePlayPause();
            } else if (selectedFileId) {
                var file = allFiles.find(function (f) { return f.id === selectedFileId; });
                if (file) playFile(file.id, file.filepath);
            }
        }
        function escapeAction() {
            var linkedPanel = document.getElementById('linked-file-panel');
            if (linkedPanel && linkedPanel.style.display !== 'none' && linkedPanel.style.display !== '') { closeLinkedFile(); return; }
            var helpModal = document.getElementById('help-modal');
            if (helpModal && helpModal.classList.contains('visible')) { closeHelp(); return; }
            if (waveSel) { clearWaveSelection(); return; }
            deselectAllFiles();
        }
        function deleteSelectedFiles() {
            if (selectedFileIds.size > 0) batchDelete();
            else if (selectedFileId) deleteFile(selectedFileId);
        }
        function markerAtPlayheadOrSelection() {
            if (waveSel && waveSel.end - waveSel.start > 0.0005) createMarkerFromSelection();
            else dropMarkerAtPlayhead();
        }

        // inInputs: also works while typing in a field
        var SHORTCUT_ACTIONS = [
            { id: 'playPause',     group: 'Transport', label: 'Play / pause (plays the selection if there is one)', keys: ['Space'], run: playOrPause },
            { id: 'marker',        group: 'Transport', label: 'Drop marker at playhead (or across the selection)', keys: ['M'], run: markerAtPlayheadOrSelection },
            { id: 'nextMarker',    group: 'Transport', label: 'Jump to next marker', keys: ['Mod+Shift+M'], run: function () { jumpToNextMarker(); } },
            { id: 'prevMarker',    group: 'Transport', label: 'Jump to previous marker', keys: ['Mod+Shift+Comma'], run: function () { jumpToPreviousMarker(); } },
            { id: 'toolSelect',    group: 'Waveform', label: 'Selector tool (drag selects)', keys: ['S', 'F7'], run: function () { setWaveTool('selector'); } },
            { id: 'toolGrab',      group: 'Waveform', label: 'Grabber tool (drag scrolls)', keys: ['G', 'F8'], run: function () { setWaveTool('grabber'); } },
            { id: 'zoomIn',        group: 'Waveform', label: 'Zoom in', keys: ['R'], run: function () { zoomWaveform(1.3); } },
            { id: 'zoomOut',       group: 'Waveform', label: 'Zoom out', keys: ['T'], run: function () { zoomWaveform(1 / 1.3); } },
            { id: 'nextFile',      group: 'Files', label: 'Next file', keys: ['Down'], run: function () { navigateFiles(1); } },
            { id: 'prevFile',      group: 'Files', label: 'Previous file', keys: ['Up'], run: function () { navigateFiles(-1); } },
            { id: 'selectAll',     group: 'Files', label: 'Select all files', keys: ['Mod+A'], run: function () { selectAllFiles(); } },
            { id: 'deselectAll',   group: 'Files', label: 'Deselect all files', keys: ['Mod+D'], run: function () { deselectAllFiles(); } },
            { id: 'deleteFiles',   group: 'Files', label: 'Remove selected file(s) from the database', keys: ['Delete', 'Backspace'], run: deleteSelectedFiles },
            { id: 'quickTag',      group: 'Files', label: 'Quick add tag', keys: ['Mod+T'], run: function () { quickAddTag(); } },
            { id: 'escape',        group: 'Files', label: 'Close window / clear selection / deselect', keys: ['Escape'], run: escapeAction },
            { id: 'searchFields',  group: 'Search', label: 'Search fields', keys: ['Mod+Shift+J'], inInputs: true, run: function () { document.getElementById('field-search').focus(); } },
            { id: 'searchFiles',   group: 'Search', label: 'Search files', keys: ['Mod+Shift+K'], inInputs: true, run: function () { document.getElementById('file-search').focus(); } },
            { id: 'searchContent', group: 'Search', label: 'Search field content', keys: ['Mod+Shift+L'], inInputs: true, run: function () { document.getElementById('content-search').focus(); } },
            { id: 'newField',      group: 'Project', label: 'New field', keys: ['Mod+Shift+N'], inInputs: true, run: function () { newField(); } },
            { id: 'importAudio',   group: 'Project', label: 'Import audio', keys: ['Mod+Shift+I'], inInputs: true, run: function () { importFiles(); } },
            { id: 'openDatabase',  group: 'Project', label: 'Open database', keys: ['Mod+Shift+O'], inInputs: true, run: function () { openDatabase(); } },
            { id: 'saveProject',   group: 'Project', label: 'Save project (export)', keys: ['Mod+S'], inInputs: true, run: function () { saveProject(); } },
            { id: 'undo',          group: 'Project', label: 'Undo', keys: ['Mod+Z'], run: function () { undo(); } },
            { id: 'redo',          group: 'Project', label: 'Redo', keys: ['Mod+Shift+Z'], run: function () { redo(); } }
        ];
        // Left to the system on purpose (copy, paste, quit, close window...)
        var RESERVED_COMBOS = ['Mod+C', 'Mod+V', 'Mod+X', 'Mod+Q', 'Mod+W', 'Mod+H', 'Mod+Comma'];

        var shortcutOverrides = {};
        var shortcutIndex = {};
        function shortcutKeys(action) {
            return shortcutOverrides.hasOwnProperty(action.id) ? shortcutOverrides[action.id] : action.keys;
        }
        function rebuildShortcutIndex() {
            shortcutIndex = {};
            SHORTCUT_ACTIONS.forEach(function (act) {
                shortcutKeys(act).forEach(function (c) { shortcutIndex[c] = act; });
            });
        }
        rebuildShortcutIndex();
        fetch('/api/settings/shortcuts')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.overrides) { shortcutOverrides = d.overrides; rebuildShortcutIndex(); renderShortcutList(); }
            })
            .catch(function () {});
        function saveShortcuts() {
            fetch('/api/settings/shortcuts', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ overrides: shortcutOverrides })
            }).catch(function () {});
        }

        var CODE_NAMES = { Comma: 'Comma', Period: 'Period', Slash: 'Slash', Semicolon: 'Semicolon', Quote: 'Quote',
                           BracketLeft: 'BracketLeft', BracketRight: 'BracketRight', Minus: 'Minus', Equal: 'Equal',
                           Backquote: 'Backquote', Backslash: 'Backslash', IntlBackslash: 'IntlBackslash' };
        var KEY_NAMES = { ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right', ' ': 'Space' };
        function comboFromEvent(e) {
            var k = e.key;
            if (k === 'Shift' || k === 'Control' || k === 'Alt' || k === 'Meta' || k === 'CapsLock' || k === 'Dead' || !k) return null;
            var name;
            var code = e.code || '';
            if (code.indexOf('Digit') === 0) name = code.slice(5);
            else if (CODE_NAMES[code]) name = CODE_NAMES[code];
            else if (code.indexOf('Key') === 0 && (e.altKey || k.length !== 1)) name = code.slice(3);
            else if (KEY_NAMES[k]) name = KEY_NAMES[k];
            else if (k.length === 1) name = k.toUpperCase();
            else name = k;
            var parts = [];
            if (IS_MAC ? e.metaKey : e.ctrlKey) parts.push('Mod');
            if (IS_MAC && e.ctrlKey) parts.push('Ctrl');
            if (!IS_MAC && e.metaKey) parts.push('Meta');
            if (e.altKey) parts.push('Alt');
            if (e.shiftKey) parts.push('Shift');
            parts.push(name);
            return parts.join('+');
        }
        var LABELS = { Mod: IS_MAC ? '⌘' : 'Ctrl', Ctrl: '⌃', Meta: 'Win', Alt: IS_MAC ? '⌥' : 'Alt', Shift: '⇧',
                       Up: '↑', Down: '↓', Left: '←', Right: '→', Comma: ',', Period: '.', Slash: '/', Semicolon: ';',
                       Quote: "'", BracketLeft: '[', BracketRight: ']', Minus: '-', Equal: '=', Backquote: '`',
                       Backslash: 'Backslash', Backspace: '⌫', Delete: 'Del', Escape: 'Esc', Enter: '↵' };
        function comboLabel(c) {
            return c.split('+').map(function (p) { return LABELS[p] || p; }).join(IS_MAC ? ' ' : ' + ');
        }

        // Capture: while customising, the next key combination goes to the chosen action.
        var shortcutCapture = null;
        var shortcutEditing = false;
        window.addEventListener('keydown', function (e) {
            if (!shortcutCapture) return;
            e.preventDefault();
            e.stopPropagation();
            if (e.key === 'Escape' && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) {
                shortcutCapture = null;
                setShortcutNote('');
                renderShortcutList();
                return;
            }
            var combo = comboFromEvent(e);
            if (!combo) return;                          // modifier on its own: keep waiting
            var target = SHORTCUT_ACTIONS.find(function (a) { return a.id === shortcutCapture; });
            shortcutCapture = null;
            if (RESERVED_COMBOS.indexOf(combo) !== -1) {
                setShortcutNote(comboLabel(combo) + ' is left to the system and can\\'t be used.');
                renderShortcutList();
                return;
            }
            var note = '';
            var owner = shortcutIndex[combo];
            if (owner && owner !== target) {
                shortcutOverrides[owner.id] = shortcutKeys(owner).filter(function (c) { return c !== combo; });
                note = comboLabel(combo) + ' moved here from "' + owner.label + '".';
            }
            var keys = shortcutKeys(target).slice();
            if (keys.indexOf(combo) === -1) keys.push(combo);
            shortcutOverrides[target.id] = keys.slice(-4);
            tidyOverrides();
            rebuildShortcutIndex();
            saveShortcuts();
            setShortcutNote(note);
            renderShortcutList();
        }, true);

        function tidyOverrides() {
            // Drop overrides that are back to the default, so defaults can evolve.
            SHORTCUT_ACTIONS.forEach(function (a) {
                var o = shortcutOverrides[a.id];
                if (o && o.length === a.keys.length && o.every(function (c, i) { return c === a.keys[i]; })) delete shortcutOverrides[a.id];
            });
        }
        function setShortcutNote(t) {
            var el = document.getElementById('sc-note');
            if (el) { el.textContent = t; el.style.display = t ? 'block' : 'none'; }
        }
        function toggleShortcutEdit() {
            shortcutEditing = !shortcutEditing;
            shortcutCapture = null;
            setShortcutNote('');
            renderShortcutList();
        }
        function resetShortcuts() {
            shortcutOverrides = {};
            shortcutCapture = null;
            rebuildShortcutIndex();
            saveShortcuts();
            setShortcutNote('All shortcuts are back to their defaults.');
            renderShortcutList();
        }
        function removeShortcut(actionId, combo) {
            var act = SHORTCUT_ACTIONS.find(function (a) { return a.id === actionId; });
            shortcutOverrides[actionId] = shortcutKeys(act).filter(function (c) { return c !== combo; });
            tidyOverrides();
            rebuildShortcutIndex();
            saveShortcuts();
            renderShortcutList();
        }
        function startShortcutCapture(actionId) {
            shortcutCapture = actionId;
            setShortcutNote('Press the new key combination. Esc cancels.');
            renderShortcutList();
        }
        function escapeHtml(t) {
            return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }
        function renderShortcutList() {
            var ul = document.getElementById('shortcut-list-keys');
            if (!ul) return;
            var editBtn = document.getElementById('sc-edit-btn');
            var resetBtn = document.getElementById('sc-reset-btn');
            if (editBtn) editBtn.textContent = shortcutEditing ? 'Done' : 'Customise';
            if (resetBtn) resetBtn.style.display = shortcutEditing ? '' : 'none';
            var html = '';
            var lastGroup = null;
            SHORTCUT_ACTIONS.forEach(function (act) {
                if (act.group !== lastGroup) {
                    html += '<li class="sc-group">' + escapeHtml(act.group) + '</li>';
                    lastGroup = act.group;
                }
                var keys = shortcutKeys(act);
                var caps = keys.map(function (c) {
                    var x = shortcutEditing ? '<button class="sc-remove" title="Remove" onclick="removeShortcut(\\'' + act.id + '\\', \\'' + c + '\\')">×</button>' : '';
                    return '<span class="shortcut-key">' + escapeHtml(comboLabel(c)) + x + '</span>';
                }).join(' ');
                if (!keys.length) caps = '<span class="sc-none">none</span>';
                if (shortcutEditing) {
                    caps += shortcutCapture === act.id
                        ? ' <span class="shortcut-key sc-waiting">press keys…</span>'
                        : ' <button class="btn btn-small sc-add" onclick="startShortcutCapture(\\'' + act.id + '\\')">+ Key</button>';
                }
                var changed = shortcutOverrides.hasOwnProperty(act.id) ? ' sc-changed' : '';
                html += '<li class="sc-row' + changed + '"><span>' + escapeHtml(act.label) + '</span><span class="sc-keys">' + caps + '</span></li>';
            });
            ul.innerHTML = html;
        }

        document.addEventListener('keydown', function (e) {
            if (shortcutCapture) return;
            var combo = comboFromEvent(e);
            if (!combo) return;
            var act = shortcutIndex[combo];
            if (!act) return;
            if (isTypingTarget() && !act.inInputs) return;   // let the field have the key
            e.preventDefault();
            act.run(e);
        });

        function navigateFiles(direction) {
            const visibleFiles = getVisibleFiles();
            if (visibleFiles.length === 0) return;
            
            if (focusedFileIndex === -1) {
                focusedFileIndex = direction > 0 ? 0 : visibleFiles.length - 1;
            } else {
                focusedFileIndex = Math.max(0, Math.min(visibleFiles.length - 1, focusedFileIndex + direction));
            }
            
            const file = visibleFiles[focusedFileIndex];
            selectFile(file.id);
            
            // Scroll into view
            const fileElement = document.querySelector(`[data-file-id="${file.id}"]`);
            if (fileElement) {
                fileElement.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
        }
        
        // Returns files in the order they're shown in the tree, skipping any
        // that live inside a collapsed group (so keyboard navigation feels right).
        function getVisibleFiles() {
            const filterLower = fileFilter ? fileFilter.toLowerCase() : '';
            const matches = (f) => !filterLower || f.filename.toLowerCase().includes(filterLower);
            // When filtering, ignore group expansion — show every match flat.
            if (filterLower) {
                return allFiles.filter(matches);
            }
            const childGroups = {};
            allGroups.forEach(g => {
                const key = g.parent_id == null ? 'root' : 'g' + g.parent_id;
                (childGroups[key] = childGroups[key] || []).push(g);
            });
            const filesByGroup = {};
            allFiles.forEach(f => {
                const key = f.group_id == null ? 'root' : 'g' + f.group_id;
                (filesByGroup[key] = filesByGroup[key] || []).push(f);
            });
            const out = [];
            const walk = (key) => {
                (childGroups[key] || []).forEach(g => {
                    if (expandedGroupIds.has(g.id)) {
                        walk('g' + g.id);
                    }
                });
                (filesByGroup[key] || []).forEach(f => out.push(f));
            };
            walk('root');
            return out;
        }
        
        function selectAllFiles() {
            selectedFileIds = new Set(allFiles.map(f => f.id));
            updateSelectionUI();
            renderFiles();
        }
        
        function deselectAllFiles() {
            selectedFileIds.clear();
            updateSelectionUI();
            renderFiles();
        }
        
        function toggleSelectAll(checked) {
            if (checked) {
                selectAllFiles();
            } else {
                deselectAllFiles();
            }
        }
        
        function quickAddTag() {
            qdPrompt(selectedFileIds.size > 0 ? 'Tag to add to the ' + selectedFileIds.size + ' selected file(s):' : 'Tag to add:', '', {title: 'Add tag', ok: 'Add'}).then(tag => {
            if (!tag) return;
            
            if (selectedFileIds.size > 0) {
                // Batch tag
                fetch('/api/files/batch/tag', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_ids: Array.from(selectedFileIds), tag})
                }).then(() => {
                    loadFiles();
                    if (selectedFileId) loadFileMetadata(selectedFileId);
                });
            } else if (selectedFileId) {
                fetch('/api/file/' + selectedFileId + '/tag', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tag})
                }).then(() => {
                    loadFiles();
                    loadFileMetadata(selectedFileId);
                });
            }
            });
        }
        
        let _lastBulkShown = false;
        function updateSelectionUI() {
            const count = selectedFileIds.size;
            document.getElementById('selection-count').textContent = count + ' selected';
            document.getElementById('batch-info').textContent = count + ' files selected';
            document.getElementById('select-all-checkbox').checked = count === allFiles.length && count > 0;
            document.getElementById('select-all-checkbox').indeterminate = count > 0 && count < allFiles.length;

            const batchPanel = document.getElementById('batch-panel');
            if (count > 0) {
                batchPanel.classList.add('visible');
            } else {
                batchPanel.classList.remove('visible');
            }

            // If we crossed the bulk-edit threshold (1 ↔ many), refresh the
            // metadata panel so the bulk banner appears or disappears.
            const bulkNow = count > 1;
            if (bulkNow !== _lastBulkShown && selectedFileId != null) {
                loadFileMetadata(selectedFileId);
            }
            _lastBulkShown = bulkNow;
        }
        
        function toggleFileSelection(fileId, event) {
            event.stopPropagation();
            
            if (event.shiftKey && selectedFileId) {
                // Shift+click: range selection
                const startIdx = allFiles.findIndex(f => f.id === selectedFileId);
                const endIdx = allFiles.findIndex(f => f.id === fileId);
                const [from, to] = [Math.min(startIdx, endIdx), Math.max(startIdx, endIdx)];
                
                for (let i = from; i <= to; i++) {
                    selectedFileIds.add(allFiles[i].id);
                }
            } else {
                if (selectedFileIds.has(fileId)) {
                    selectedFileIds.delete(fileId);
                } else {
                    selectedFileIds.add(fileId);
                }
            }
            
            updateSelectionUI();
            renderFiles();
        }
        
        function showHelp() {
            renderShortcutList();
            document.getElementById('help-modal').classList.add('visible');
        }
        
        function closeHelp() {
            shortcutCapture = null;
            shortcutEditing = false;
            setShortcutNote('');
            document.getElementById('help-modal').classList.remove('visible');
        }
        
        function loadFields() {
            fetch('/api/fields')
                .then(r => r.json())
                .then(fields => {
                    allFields = fields;
                    renderFields();
                    
                    // Update query field dropdowns
                    const fieldOptions = fields.map(f => `<option value="${f.id}">${f.name}</option>`).join('');
                    document.querySelectorAll('[id^="qfield-"]').forEach(sel => {
                        const current = sel.value;
                        sel.innerHTML = '<option value="">Select field...</option>' + fieldOptions;
                        sel.value = current;
                    });
                    
                    // Update sort dropdown
                    const sortField = document.getElementById('sort-field');
                    if (sortField) {
                        const currentSort = sortField.value;
                        sortField.innerHTML = '<option value="">Filename</option>' + fieldOptions;
                        sortField.value = currentSort;
                    }
                });
        }
        
        function renderFields() {
            const container = document.getElementById('fields-list');
            let fields = allFields;
            
            if (fieldFilter) {
                fields = fields.filter(f => f.name.toLowerCase().includes(fieldFilter.toLowerCase()));
            }
            
            if (fields.length === 0) {
                container.innerHTML = '<div class="empty-state">No fields yet</div>';
            } else {
                container.innerHTML = fields.map(f => `
                    <div class="field-item" onclick="editField(${f.id})">
                        <div style="display: flex; justify-content: space-between; align-items: start;">
                            <div>
                                <div style="font-weight: 500;">${f.name}</div>
                                <div style="font-size: 11px; color: var(--grafite);">${f.type}${f.type === 'parameter' ? ' (' + f.min + '-' + f.max + ')' : ''}</div>
                            </div>
                            <button class="btn btn-small btn-danger" onclick="event.stopPropagation(); deleteField(${f.id})" style="padding: 2px 6px;">×</button>
                        </div>
                    </div>
                `).join('');
            }
        }
        
        function filterFields(query) {
            fieldFilter = query;
            renderFields();
        }
        
        function filterFiles(query) {
            fileFilter = query;
            focusedFileIndex = -1;
            renderFiles();
        }
        
        function loadFiles() {
            // Load groups and files together so the tree always renders consistently.
            Promise.all([
                fetch('/api/files').then(r => r.json()),
                fetch('/api/groups').then(r => r.json()),
            ]).then(([files, groups]) => {
                allFiles = files;
                allGroups = groups;
                // Drop any expanded ids that no longer exist
                const liveIds = new Set(groups.map(g => g.id));
                expandedGroupIds.forEach(id => { if (!liveIds.has(id)) expandedGroupIds.delete(id); });
                renderFiles();
                loadFileDurations();
            });
        }

        // Fetch per-file durations (cheap header reads) and re-render the list so
        // each row shows its length on the right. Failures are non-fatal.
        function loadFileDurations() {
            fetch('/api/files/durations')
                .then(r => r.json())
                .then(map => { fileDurations = map || {}; renderFiles(); })
                .catch(() => {});
        }

        // Format seconds as m:ss (or h:mm:ss for long files).
        function formatDuration(sec) {
            if (sec == null || isNaN(sec)) return '';
            sec = Math.round(sec);
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = sec % 60;
            if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
            return m + ':' + String(s).padStart(2, '0');
        }

        function _escapeHtml(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        // Render <option> tags for the "Move to group…" dropdown, indented to
        // reflect nesting depth.
        function _renderGroupSelectOptions() {
            const childGroups = {};
            allGroups.forEach(g => {
                const key = g.parent_id == null ? 'root' : 'g' + g.parent_id;
                (childGroups[key] = childGroups[key] || []).push(g);
            });
            const out = [];
            const walk = (key, depth) => {
                (childGroups[key] || []).forEach(g => {
                    const indent = '  '.repeat(depth);
                    out.push(`<option value="${g.id}">${indent}${_escapeHtml(g.name)}</option>`);
                    walk('g' + g.id, depth + 1);
                });
            };
            walk('root', 0);
            return out.join('');
        }

        function _fileItemHtml(f, depth, focusedId) {
            const indent = (depth * 16) + 'px';
            const safePath = f.filepath.replace(/'/g, "\\'");
            return `
                <div class="file-item ${selectedFileId === f.id ? 'active' : ''} ${selectedFileIds.has(f.id) ? 'selected' : ''} ${focusedId === f.id ? 'focused' : ''}"
                     data-file-id="${f.id}" onclick="selectFile(${f.id})" style="margin-left:${indent};">
                    <div style="display: flex; align-items: center;">
                        <input type="checkbox" class="file-checkbox" ${selectedFileIds.has(f.id) ? 'checked' : ''}
                            onclick="toggleFileSelection(${f.id}, event)">
                        <button class="play-btn" onclick="event.stopPropagation(); playFile(${f.id}, '${safePath}')">
                            ${currentPlayingId === f.id ? '⏸' : '▶'}
                        </button>
                        <div style="flex: 1; min-width: 0;">
                            <div class="file-name" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${_escapeHtml(f.filename)}</div>
                            ${f.tags.length > 0 ? `<div class="file-tags">${f.tags.map(t => `<span class="tag">${_escapeHtml(t)}</span>`).join('')}</div>` : ''}
                        </div>
                        <span class="file-duration">${formatDuration(fileDurations[f.id])}</span>
                    </div>
                </div>
            `;
        }

        function _groupRowHtml(g, depth, fileCount) {
            const expanded = expandedGroupIds.has(g.id);
            const indent = (depth * 16) + 'px';
            return `
                <div class="group-row" style="margin-left:${indent};" data-group-id="${g.id}" onclick="toggleGroupExpansion(${g.id})">
                    <span class="group-chevron">${expanded ? '▾' : '▸'}</span>
                    <span class="group-name" id="group-name-${g.id}"
                          ondblclick="event.stopPropagation(); startRenameGroup(${g.id})">${_escapeHtml(g.name)}</span>
                    <span class="group-count">${fileCount}</span>
                    <span class="group-actions" onclick="event.stopPropagation();">
                        <button onclick="newSubGroup(${g.id})" title="New subgroup">＋</button>
                        <button onclick="moveSelectionToGroup(${g.id})" title="Move selection here">⇲</button>
                        <button onclick="startRenameGroup(${g.id})" title="Rename">Rename</button>
                        <button onclick="deleteGroup(${g.id})" title="Delete group">×</button>
                    </span>
                </div>
            `;
        }

        function renderFiles() {
            const container = document.getElementById('files-list');

            // When a filename filter is active, render a flat list of matches and
            // ignore group structure (so users always find what they searched).
            if (fileFilter) {
                const filtered = allFiles.filter(f => f.filename.toLowerCase().includes(fileFilter.toLowerCase()));
                if (filtered.length === 0) {
                    container.innerHTML = '<div class="empty-state">No files match your search</div>';
                } else {
                    const focusedIdF = (focusedFileIndex >= 0 && focusedFileIndex < filtered.length) ? filtered[focusedFileIndex].id : null;
                    container.innerHTML =
                        `<div style="margin-bottom:6px;"><button class="btn btn-small btn-secondary" onclick="newGroup()">+ New group</button></div>` +
                        filtered.map(f => _fileItemHtml(f, 0, focusedIdF)).join('');
                }
                updateSelectionUI();
                return;
            }

            // Build child-group and file-by-group indexes for fast tree assembly.
            const childGroups = {};
            allGroups.forEach(g => {
                const key = g.parent_id == null ? 'root' : 'g' + g.parent_id;
                (childGroups[key] = childGroups[key] || []).push(g);
            });
            const filesByGroup = {};
            allFiles.forEach(f => {
                const key = f.group_id == null ? 'root' : 'g' + f.group_id;
                (filesByGroup[key] = filesByGroup[key] || []).push(f);
            });
            // Recursive count (group total = direct files + descendants' files)
            const recursiveCount = (gid) => {
                let n = (filesByGroup['g' + gid] || []).length;
                (childGroups['g' + gid] || []).forEach(c => n += recursiveCount(c.id));
                return n;
            };

            const visible = getVisibleFiles();
            const focusedId = (focusedFileIndex >= 0 && focusedFileIndex < visible.length) ? visible[focusedFileIndex].id : null;

            const out = [];
            out.push(`<div style="margin-bottom:6px; display:flex; gap:6px;">
                <button class="btn btn-small btn-secondary" onclick="newGroup()">+ New group</button>
                <button class="btn btn-small btn-secondary" onclick="moveSelectionToGroup(null)" title="Move selected files out of any group">⇱ Ungroup selection</button>
            </div>`);

            const walk = (key, depth) => {
                (childGroups[key] || []).forEach(g => {
                    out.push(_groupRowHtml(g, depth, recursiveCount(g.id)));
                    if (expandedGroupIds.has(g.id)) {
                        walk('g' + g.id, depth + 1);
                        const inGroup = filesByGroup['g' + g.id] || [];
                        if (inGroup.length === 0 && (childGroups['g' + g.id] || []).length === 0) {
                            out.push(`<div class="group-empty" style="margin-left:${(depth + 1) * 16}px;">empty</div>`);
                        }
                        inGroup.forEach(f => out.push(_fileItemHtml(f, depth + 1, focusedId)));
                    }
                });
                if (key === 'root') {
                    (filesByGroup['root'] || []).forEach(f => out.push(_fileItemHtml(f, 0, focusedId)));
                }
            };
            walk('root', 0);

            if (allFiles.length === 0 && allGroups.length === 0) {
                container.innerHTML = '<div class="empty-state">No files imported</div>';
            } else {
                container.innerHTML = out.join('');
            }

            updateSelectionUI();
        }

        // ---- Group operations -----------------------------------------------
        function toggleGroupExpansion(groupId) {
            if (expandedGroupIds.has(groupId)) expandedGroupIds.delete(groupId);
            else expandedGroupIds.add(groupId);
            renderFiles();
        }

        function newGroup() {
            qdPrompt('Name of the new group:', '', {title: 'New group', ok: 'Create'}).then(name => {
            if (!name || !name.trim()) return;
            fetch('/api/groups', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name.trim(), parent_id: null})
            }).then(r => r.json()).then(res => {
                if (!res.success) { alert('Could not create group: ' + (res.error || 'unknown')); return; }
                if (res.id) expandedGroupIds.add(res.id);
                loadFiles();
            });
            });
        }

        function newSubGroup(parentId) {
            qdPrompt('Name of the new subgroup:', '', {title: 'New subgroup', ok: 'Create'}).then(name => {
            if (!name || !name.trim()) return;
            fetch('/api/groups', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name.trim(), parent_id: parentId})
            }).then(r => r.json()).then(res => {
                if (!res.success) { alert('Could not create subgroup: ' + (res.error || 'unknown')); return; }
                expandedGroupIds.add(parentId);
                if (res.id) expandedGroupIds.add(res.id);
                loadFiles();
            });
            });
        }

        function startRenameGroup(groupId) {
            const span = document.getElementById('group-name-' + groupId);
            if (!span) return;
            const original = span.textContent;
            span.setAttribute('contenteditable', 'true');
            span.focus();
            // Select all
            const range = document.createRange();
            range.selectNodeContents(span);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            const finish = (commit) => {
                span.removeAttribute('contenteditable');
                const newName = span.textContent.trim();
                if (!commit || !newName || newName === original) {
                    span.textContent = original;
                    return;
                }
                fetch('/api/groups/' + groupId, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: newName})
                }).then(r => r.json()).then(res => {
                    if (!res.success) {
                        alert('Could not rename: ' + (res.error || 'unknown'));
                        span.textContent = original;
                        return;
                    }
                    loadFiles();
                });
            };
            span.onkeydown = (e) => {
                if (e.key === 'Enter') { e.preventDefault(); span.blur(); }
                else if (e.key === 'Escape') { span.textContent = original; span.blur(); }
            };
            span.onblur = () => finish(true);
        }

        function deleteGroup(groupId) {
            const g = allGroups.find(x => x.id === groupId);
            const label = g ? g.name : 'this group';
            qdConfirm(`Delete group "${label}"? Its files and subgroups move up one level.`, {title: 'Delete group', ok: 'Delete', danger: true}).then(ok => {
            if (!ok) return;
            fetch('/api/groups/' + groupId, {method: 'DELETE'})
                .then(r => r.json()).then(res => {
                    if (!res.success) { alert('Delete failed: ' + (res.error || 'unknown')); return; }
                    expandedGroupIds.delete(groupId);
                    loadFiles();
                });
            });
        }

        function moveSelectionToGroup(groupId) {
            // Use the active edit target: bulk selection if >1, else focused file.
            const ids = (typeof getActiveTargetIds === 'function') ? getActiveTargetIds() : (selectedFileId != null ? [selectedFileId] : []);
            if (ids.length === 0) {
                alert('Select one or more files first.');
                return;
            }
            fetch('/api/files/group', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_ids: ids, group_id: groupId})
            }).then(() => {
                if (groupId != null) expandedGroupIds.add(groupId);
                loadFiles();
            });
        }
        // ---------------------------------------------------------------------
        
        function selectFile(fileId) {
            selectedFileId = fileId;
            // Index focus into the *visible* (tree-order) list so keyboard nav stays consistent.
            focusedFileIndex = getVisibleFiles().findIndex(f => f.id === fileId);
            renderFiles();
            loadFileMetadata(fileId);
            document.getElementById('file-stats-btn').style.display = 'inline-block';
        }
        
        function deleteFile(fileId) {
            qdConfirm('Remove this file from the database? The audio file on disk is not touched.', {title: 'Remove file', ok: 'Remove', danger: true}).then(ok => { if (!ok) return;
            
            fetch('/api/file/' + fileId, {
                method: 'DELETE'
            }).then(() => {
                if (selectedFileId === fileId) {
                    selectedFileId = null;
                    document.getElementById('selected-file-name').textContent = 'No file selected';
                    document.getElementById('metadata-panel').innerHTML = '<div class="empty-state">Select a file to edit metadata</div>';
                }
                selectedFileIds.delete(fileId);
                loadFiles();
            });
            });
        }
        
        function deleteField(fieldId) {
            qdConfirm('Delete this field? Its values on every file will be lost.', {title: 'Delete field', ok: 'Delete', danger: true}).then(ok => { if (!ok) return;
            
            fetch('/api/field/' + fieldId, {
                method: 'DELETE'
            }).then(() => {
                loadFields();
                if (selectedFileId) loadFileMetadata(selectedFileId);
            });
            });
        }
        
        function editField(fieldId) {
            const field = allFields.find(f => f.id === fieldId);
            if (!field) return;
            qdPrompt(`Rename field "${field.name}" to:`, field.name, {title: 'Rename field', ok: 'Rename'}).then(newName => {
            if (!newName || newName.trim() === '' || newName.trim() === field.name) return;
            fetch('/api/field/' + fieldId, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: newName.trim()})
            }).then(r => r.json()).then(res => {
                if (!res.success) {
                    alert('Could not rename field: ' + (res.error || 'unknown error'));
                    return;
                }
                loadFields();
                if (selectedFileId) loadFileMetadata(selectedFileId);
            });
            });
        }
        
        function playFile(fileId, filepath) {
            const file = allFiles.find(f => f.id === fileId);
            
            if (currentPlayingId === fileId) {
                togglePlayPause();
            } else {
                // Use the audio serving route instead of file://
                audioPlayer.src = '/api/audio/' + fileId;
                audioPlayer.play()
                    .then(() => {
                        currentPlayingId = fileId;
                        showPlayerBar(file ? file.filename : 'Unknown');
                        loadWaveform(fileId);
                        loadAudioInfo(fileId);
                        renderFiles();
                    })
                    .catch(err => {
                        console.error('Playback error:', err);
                        alert('Cannot play audio file: ' + err.message);
                    });
            }
            renderFiles();
        }
        
        function showPlayerBar(filename) {
            document.getElementById('player-bar').classList.add('visible');
            document.getElementById('wave-strip').classList.add('has-audio');
            document.getElementById('player-filename').textContent = filename;
            document.getElementById('player-play-btn').textContent = '⏸';
            updatePlayerMarkers();
        }
        
        function hidePlayerBar() {
            document.getElementById('player-bar').classList.remove('visible');
            document.getElementById('wave-strip').classList.remove('has-audio');
        }
        
        function togglePlayPause() {
            if (audioPlayer.paused) {
                if (waveSel && waveSel.end - waveSel.start > 0.0005) playSelection();
                else audioPlayer.play();
                document.getElementById('player-play-btn').textContent = '⏸';
            } else {
                audioPlayer.pause();
                document.getElementById('player-play-btn').textContent = '▶';
            }
        }
        
        function skipTime(seconds) {
            audioPlayer.currentTime += seconds;
        }
        
        // ============================================================
        // Unified waveform pointer interactions
        //   plain drag            -> scroll/pan the visible window
        //   plain click (no drag) -> seek the playhead to the click position
        //   Cmd  + click/drag     -> position / scrub the playhead
        //   Option + click/drag   -> grab the nearest marker edge (start/end) and move it
        //   Ctrl + drag (left/right) -> zoom out / in, anchored where the drag began
        // (The old mouse-wheel zoom was removed; use the r / t keys or Ctrl+drag.)
        // ============================================================
        const MARKER_EDGE_GRAB_PX = 8;
        // ---- Edit tools (Pro Tools style): Grabber = drag pans, Selector = drag selects ----
        var waveTool = 'grabber';
        try { if (localStorage.getItem('earmark_wave_tool') === 'selector') waveTool = 'selector'; } catch (e) {}
        var waveSel = null;              // {start, end} in seconds, or null
        var waveSelPlayRAF = null;       // rAF id while playing a selection

        function setWaveTool(tool) {
            waveTool = (tool === 'selector') ? 'selector' : 'grabber';
            try { localStorage.setItem('earmark_wave_tool', waveTool); } catch (e) {}
            updateWaveToolUI();
        }

        function updateWaveToolUI() {
            var g = document.getElementById('tool-grabber-btn');
            var s = document.getElementById('tool-selector-btn');
            if (g) g.className = 'tool-btn' + (waveTool === 'grabber' ? ' active' : '');
            if (s) s.className = 'tool-btn' + (waveTool === 'selector' ? ' active' : '');
            var cont = document.getElementById('player-progress-container');
            if (cont) {
                if (waveTool === 'selector') cont.classList.add('tool-selector');
                else cont.classList.remove('tool-selector');
            }
            var name = document.getElementById('tool-name-display');
            if (name) name.textContent = waveTool === 'selector' ? 'Selector' : 'Grabber';
        }

        function setWaveSelection(a, b) {
            var dur = (audioPlayer && audioPlayer.duration) || 0;
            var s = Math.max(0, Math.min(a, b));
            var e = Math.min(dur || Math.max(a, b), Math.max(a, b));
            waveSel = { start: s, end: e };
            updateSelectionOverlay();
            updateSelectionInfo();
        }

        function clearWaveSelection() {
            waveSel = null;
            stopSelectionPlayback();
            updateSelectionOverlay();
            updateSelectionInfo();
        }

        function updateSelectionOverlay() {
            var el = document.getElementById('wave-selection');
            if (!el) return;
            if (!waveSel || !audioPlayer || !audioPlayer.duration) { el.style.display = 'none'; return; }
            var dur = audioPlayer.duration;
            var a = (waveSel.start / dur - waveformOffset) * waveformZoom * 100;
            var b = (waveSel.end / dur - waveformOffset) * waveformZoom * 100;
            if (b < 0 || a > 100) { el.style.display = 'none'; return; }
            var ca = Math.max(0, a), cb = Math.min(100, b);
            el.style.display = 'block';
            el.style.left = ca + '%';
            el.style.width = Math.max(cb - ca, 0.15) + '%';
            el.style.borderLeftStyle = (a >= 0) ? 'solid' : 'none';
            el.style.borderRightStyle = (b <= 100) ? 'solid' : 'none';
        }

        function updateSelectionInfo() {
            var box = document.getElementById('wave-sel-info');
            if (!box) return;
            if (!waveSel || waveSel.end - waveSel.start < 0.0005) { box.style.display = 'none'; return; }
            var len = waveSel.end - waveSel.start;
            document.getElementById('wave-sel-text').textContent =
                secondsToHMS(waveSel.start) + ' → ' + secondsToHMS(waveSel.end) +
                '  (' + (len >= 1 ? len.toFixed(3) + ' s' : (len * 1000).toFixed(1) + ' ms') + ')';
            box.style.display = 'inline-flex';
        }

        function zoomToSelection() {
            if (!waveSel || !audioPlayer || !audioPlayer.duration) return;
            var dur = audioPlayer.duration;
            var span = (waveSel.end - waveSel.start) / dur;
            if (span <= 0) return;
            waveformZoom = Math.max(MIN_WAVEFORM_ZOOM, Math.min(MAX_WAVEFORM_ZOOM, 1 / (span * 1.1)));
            waveformOffset = waveSel.start / dur - (1 / waveformZoom - span) / 2;
            clampWaveformOffset();
            redrawWaveformView();
            scheduleWaveformRefetch();
        }

        // Play the selection once, then return the playhead to its start (Pro Tools behaviour).
        function playSelection() {
            if (!waveSel || !audioPlayer || !audioPlayer.duration) return;
            stopSelectionPlayback();
            audioPlayer.currentTime = waveSel.start;
            audioPlayer.play();
            var endAt = waveSel.end, startAt = waveSel.start;
            function watch() {
                if (!waveSel || audioPlayer.paused) { waveSelPlayRAF = null; return; }
                if (audioPlayer.currentTime >= endAt) {
                    audioPlayer.pause();
                    audioPlayer.currentTime = startAt;
                    waveSelPlayRAF = null;
                    return;
                }
                waveSelPlayRAF = requestAnimationFrame(watch);
            }
            waveSelPlayRAF = requestAnimationFrame(watch);
        }

        function stopSelectionPlayback() {
            if (waveSelPlayRAF) { cancelAnimationFrame(waveSelPlayRAF); waveSelPlayRAF = null; }
        }

        // Create a marker spanning the current selection in the active markers field.
        function createMarkerFromSelection() {
            if (!waveSel) return;
            if (!currentPlayingId || !currentMarkersFieldId) {
                alert('Select a file with a markers field first, then play it.');
                return;
            }
            var wasPlaying = !audioPlayer.paused;
            audioPlayer.pause();
            var selStart = waveSel.start, selEnd = waveSel.end;
            qdPrompt('Label for the marker ' + secondsToHMS(selStart) + ' → ' + secondsToHMS(selEnd) + ':', '', {title: 'New marker', ok: 'Add'}).then(function (label) {
            if (!label) { if (wasPlaying) audioPlayer.play(); return; }
            var startStr = secondsToHMS(selStart);
            var endStr = secondsToHMS(Math.max(selEnd, selStart + 0.001));
            var fileId = currentPlayingId, fieldId = currentMarkersFieldId;
            fetch('/api/file/' + fileId)
                .then(function (r) { return r.json(); })
                .then(function (file) {
                    var field = null;
                    for (var i = 0; i < allFields.length; i++) if (allFields[i].id === fieldId) field = allFields[i];
                    var markers = [];
                    try { markers = (field && file.fields[field.name]) ? JSON.parse(file.fields[field.name]) : []; } catch (e) { markers = []; }
                    markers.push({ start: startStr, end: endStr, label: label.trim(), color: 'Yellow' });
                    return fetch('/api/file/' + fileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(function () {
                        currentMarkers = markers;
                        if (markerFieldsState[fieldId]) markerFieldsState[fieldId].markers = markers;
                        updatePlayerMarkers();
                        if (selectedFileId === fileId) loadFileMetadata(selectedFileId);
                        if (wasPlaying) audioPlayer.play();
                    });
                });
            });
        }

        // Trackpad two-finger horizontal swipe pans the zoomed waveform (vertical scroll is left alone).
        (function initWaveformWheelPan() {
            var cont = document.getElementById('player-progress-container');
            if (!cont) return;
            cont.addEventListener('wheel', function (e) {
                if (waveformZoom <= 1) return;
                if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
                e.preventDefault();
                waveformOffset += (e.deltaX / cont.offsetWidth) / waveformZoom;
                clampWaveformOffset();
                requestWaveformRedraw();
                scheduleWaveformRefetch();
            }, { passive: false });
        })();
        updateWaveToolUI();
        updateWaveformDbButton();

        let waveInteraction = null;

        function seekToClientX(clientX) {
            if (!audioPlayer || !audioPlayer.duration) return;
            const container = document.getElementById('player-progress-container');
            const rect = container.getBoundingClientRect();
            const screenFrac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
            const absFrac = waveformOffset + screenFrac / waveformZoom;
            audioPlayer.currentTime = Math.max(0, Math.min(1, absFrac)) * audioPlayer.duration;
        }

        function redrawWaveformView() {
            if (currentWaveformPeaks) drawWaveform(currentWaveformPeaks);
            updatePlayerMarkers();
            updatePlayheadPosition();
        }

        // Coalesce redraws to one per animation frame. Dragging fires mousemove
        // 100+×/sec, and each redraw rebuilds the markers DOM + repaints the canvas;
        // without this, that teardown-per-event is what makes panning/zooming jank.
        let _waveRedrawPending = false;
        function requestWaveformRedraw() {
            if (_waveRedrawPending) return;
            _waveRedrawPending = true;
            requestAnimationFrame(() => {
                _waveRedrawPending = false;
                redrawWaveformView();
            });
        }

        // Find the closest visible marker start/end edge to a screen-x position.
        // Returns {fieldId, markerIndex, which} within MARKER_EDGE_GRAB_PX, else null.
        function findNearestMarkerEdge(clientX) {
            if (!audioPlayer || !audioPlayer.duration) return null;
            const container = document.getElementById('player-progress-container');
            const rect = container.getBoundingClientRect();
            const xFrac = (clientX - rect.left) / rect.width;
            const dur = audioPlayer.duration;
            let best = null;
            const consider = (fieldId, idx, which, edgeAbsFrac) => {
                const edgeScreenFrac = (edgeAbsFrac - waveformOffset) * waveformZoom;
                if (edgeScreenFrac < 0 || edgeScreenFrac > 1) return; // edge off-screen
                const distPx = Math.abs(edgeScreenFrac - xFrac) * rect.width;
                if (distPx <= MARKER_EDGE_GRAB_PX && (!best || distPx < best.distPx)) {
                    best = { fieldId, markerIndex: idx, which, distPx };
                }
            };
            const fieldIds = Object.keys(markerFieldsState);
            if (fieldIds.length > 0) {
                fieldIds.forEach(fid => {
                    const st = markerFieldsState[fid];
                    if (!st || !st.visible) return;
                    st.markers.forEach((m, idx) => {
                        consider(parseInt(fid), idx, 'start', timeToSeconds(m.start) / dur);
                        consider(parseInt(fid), idx, 'end',   timeToSeconds(m.end)   / dur);
                    });
                });
            } else if (currentMarkers.length > 0 && currentMarkersFieldId != null) {
                currentMarkers.forEach((m, idx) => {
                    consider(currentMarkersFieldId, idx, 'start', timeToSeconds(m.start) / dur);
                    consider(currentMarkersFieldId, idx, 'end',   timeToSeconds(m.end)   / dur);
                });
            }
            return best;
        }

        (function initWaveformInteractions() {
            const container = document.getElementById('player-progress-container');
            if (!container) return;

            // Ctrl+click would otherwise raise the macOS context menu mid-zoom.
            container.addEventListener('contextmenu', (e) => e.preventDefault());

            container.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                if (!audioPlayer || !audioPlayer.duration) return;
                const rect = container.getBoundingClientRect();

                if (e.altKey) {
                    // Option: grab nearest marker edge; if none is close, do nothing.
                    const hit = findNearestMarkerEdge(e.clientX);
                    if (hit) startMarkerDrag(e, hit.fieldId, hit.markerIndex, hit.which);
                    e.preventDefault();
                    return;
                }

                if (e.shiftKey) {
                    // Shift+drag zooms. The gesture locks to whichever axis you move
                    // first: horizontal = time zoom, vertical = amplitude zoom.
                    const anchorScreenFrac = (e.clientX - rect.left) / rect.width;
                    waveInteraction = {
                        mode: 'zoom',
                        axis: null,
                        startX: e.clientX,
                        startY: e.clientY,
                        startZoom: waveformZoom,
                        startVScale: waveformVScale,
                        anchorScreenFrac,
                        anchorAbsFrac: waveformOffset + anchorScreenFrac / waveformZoom,
                    };
                } else if (e.metaKey) {
                    // Cmd: position + scrub the playhead.
                    waveInteraction = { mode: 'playhead' };
                    seekToClientX(e.clientX);
                } else if (waveTool === 'selector') {
                    // Selector tool: drag selects a range; a plain click places the playhead.
                    waveInteraction = {
                        mode: 'select',
                        startX: e.clientX,
                        anchorSec: clientXToSeconds(e.clientX),
                        moved: false,
                    };
                } else {
                    // Grabber tool / no modifier: pan if the mouse moves, else seek on release (a click).
                    waveInteraction = {
                        mode: 'pan',
                        startX: e.clientX,
                        startOffset: waveformOffset,
                        moved: false,
                    };
                    container.style.cursor = 'grabbing';
                }
                document.body.style.userSelect = 'none';
                e.preventDefault();
            });

            document.addEventListener('mousemove', (e) => {
                if (!waveInteraction) return;
                const rect = container.getBoundingClientRect();

                if (waveInteraction.mode === 'playhead') {
                    seekToClientX(e.clientX);
                } else if (waveInteraction.mode === 'select') {
                    if (Math.abs(e.clientX - waveInteraction.startX) > 3) waveInteraction.moved = true;
                    if (!waveInteraction.moved) return;
                    // Dragging past either edge while zoomed scrolls the view.
                    if (waveformZoom > 1 && (e.clientX < rect.left || e.clientX > rect.right)) {
                        var over = e.clientX < rect.left ? (e.clientX - rect.left) : (e.clientX - rect.right);
                        waveformOffset += (over / rect.width) * 0.15 / waveformZoom;
                        clampWaveformOffset();
                        requestWaveformRedraw();
                    }
                    setWaveSelection(waveInteraction.anchorSec, clientXToSeconds(e.clientX));
                } else if (waveInteraction.mode === 'pan') {
                    const dx = e.clientX - waveInteraction.startX;
                    if (Math.abs(dx) > 3) waveInteraction.moved = true;
                    // Grab semantics: the waveform follows the cursor.
                    const deltaFrac = dx / rect.width;
                    waveformOffset = waveInteraction.startOffset - deltaFrac / waveformZoom;
                    clampWaveformOffset();
                    requestWaveformRedraw();
                } else if (waveInteraction.mode === 'zoom') {
                    const dx = e.clientX - waveInteraction.startX;
                    const dy = waveInteraction.startY - e.clientY;
                    // Lock to the dominant axis once the drag clears a small threshold.
                    if (!waveInteraction.axis) {
                        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
                            waveInteraction.axis = Math.abs(dx) >= Math.abs(dy) ? 'h' : 'v';
                        } else {
                            return;
                        }
                    }
                    if (waveInteraction.axis === 'h') {
                        // Drag right -> zoom in, left -> zoom out (~200px per 2x),
                        // anchored under the point where the drag began.
                        const factor = Math.pow(2, dx / 200);
                        waveformZoom = Math.max(MIN_WAVEFORM_ZOOM,
                            Math.min(MAX_WAVEFORM_ZOOM, waveInteraction.startZoom * factor));
                        waveformOffset = waveInteraction.anchorAbsFrac
                            - waveInteraction.anchorScreenFrac / waveformZoom;
                        clampWaveformOffset();
                    } else {
                        // Drag up -> taller (more gain), down -> shorter (~150px per 2x).
                        const factor = Math.pow(2, dy / 150);
                        waveformVScale = Math.max(MIN_WAVEFORM_VSCALE,
                            Math.min(MAX_WAVEFORM_VSCALE, waveInteraction.startVScale * factor));
                    }
                    requestWaveformRedraw();
                }
            });

            document.addEventListener('mouseup', (e) => {
                if (!waveInteraction) return;
                const wi = waveInteraction;
                waveInteraction = null;
                container.style.cursor = '';
                document.body.style.userSelect = '';
                if (wi.mode === 'select') {
                    if (!wi.moved) {
                        clearWaveSelection();       // click = place the playhead
                        seekToClientX(e.clientX);
                    } else if (waveSel && audioPlayer && audioPlayer.duration) {
                        audioPlayer.currentTime = waveSel.start;   // playhead to selection start
                        scheduleWaveformRefetch();
                    }
                    return;
                }
                if (wi.mode === 'zoom' && !wi.axis && waveTool === 'selector') {
                    // Shift+click (no drag) with the Selector extends the selection.
                    var t = clientXToSeconds(e.clientX);
                    if (waveSel) {
                        if (Math.abs(t - waveSel.start) < Math.abs(t - waveSel.end)) setWaveSelection(t, waveSel.end);
                        else setWaveSelection(waveSel.start, t);
                    } else if (audioPlayer && audioPlayer.duration) {
                        setWaveSelection(audioPlayer.currentTime, t);
                    }
                    return;
                }
                if (wi.mode === 'pan' && !wi.moved) {
                    seekToClientX(e.clientX);   // it was a click, not a drag -> seek
                } else if (wi.mode === 'pan' || (wi.mode === 'zoom' && wi.axis === 'h')) {
                    scheduleWaveformRefetch();  // fetch sharper peaks for the new window
                }
            });
        })();

        function formatTimeHMS(seconds) {
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            if (h > 0) {
                return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
            }
            return `${m}:${s.toString().padStart(2, '0')}`;
        }
        
        // Position the playhead and progress-fill relative to the visible (zoomed) window
        function updatePlayheadPosition(t) {
            if (!audioPlayer || !audioPlayer.duration) return;
            if (t === undefined) t = audioPlayer.currentTime;
            const absFrac = t / audioPlayer.duration;
            // Playhead: position within visible window, clamp to [-5,105] so it visibly
            // "leaves" the viewport when out of range rather than snapping to 0 or 100.
            let screenPct = (absFrac - waveformOffset) * waveformZoom * 100;
            const playhead = document.getElementById('player-playhead');
            if (playhead) {
                if (screenPct < -5 || screenPct > 105) {
                    playhead.style.display = 'none';
                } else {
                    playhead.style.display = '';
                    playhead.style.left = screenPct + '%';
                }
            }
            // Progress fill: from left edge of view to playhead
            const fillPct = Math.max(0, Math.min(100, screenPct));
            const fill = document.getElementById('player-progress');
            if (fill) fill.style.width = fillPct + '%';
        }

        // Smooth playhead: while playing, move it on every screen frame instead of
        // only on 'timeupdate' (which fires ~4 times a second and makes it tick).
        // If the reported time hasn't advanced since the last frame, extrapolate
        // from the wall clock so the line glides between updates.
        var playheadRAF = null;
        var phLastMedia = -1;
        var phLastWall = 0;

        function playheadFrame(now) {
            if (!audioPlayer || audioPlayer.paused) {
                playheadRAF = null;
                updatePlayheadPosition();
                return;
            }
            if (!audioPlayer.duration) {
                // Length not known yet (file still loading): wait for the next frame.
                playheadRAF = requestAnimationFrame(playheadFrame);
                return;
            }
            var t = audioPlayer.currentTime;
            if (t !== phLastMedia) {
                phLastMedia = t;
                phLastWall = now;
            } else {
                var ahead = (now - phLastWall) / 1000 * (audioPlayer.playbackRate || 1);
                if (ahead > 0.5) ahead = 0.5;
                t = phLastMedia + ahead;
            }
            if (t > audioPlayer.duration) t = audioPlayer.duration;
            updatePlayheadPosition(t);
            followPlayheadIfNeeded();
            playheadRAF = requestAnimationFrame(playheadFrame);
        }

        function startPlayheadAnimation() {
            phLastMedia = -1;
            if (!playheadRAF) playheadRAF = requestAnimationFrame(playheadFrame);
        }

        function stopPlayheadAnimation() {
            if (playheadRAF) { cancelAnimationFrame(playheadRAF); playheadRAF = null; }
            updatePlayheadPosition();
        }

        // Update player progress (counter text; playhead only when not animating)
        audioPlayer.addEventListener('timeupdate', () => {
            if (audioPlayer.duration) {
                if (!playheadRAF) {
                    updatePlayheadPosition();
                    followPlayheadIfNeeded();
                }
                document.getElementById('player-current-time').textContent = formatTimeHMS(audioPlayer.currentTime);
            }
        });

        audioPlayer.addEventListener('seeked', () => { phLastMedia = -1; });
        
        audioPlayer.addEventListener('ended', () => {
            stopPlayheadAnimation();
            currentPlayingId = null;
            document.getElementById('player-play-btn').textContent = '▶';
            renderFiles();
        });
        
        audioPlayer.addEventListener('pause', () => {
            stopPlayheadAnimation();
            document.getElementById('player-play-btn').textContent = '▶';
        });
        
        audioPlayer.addEventListener('play', () => {
            startPlayheadAnimation();
            document.getElementById('player-play-btn').textContent = '⏸';
        });
        
function updatePlayerMarkers() {
            const markersContainer = document.getElementById('player-markers');
            markersContainer.innerHTML = '';
            updateSelectionOverlay();
 
            if (!audioPlayer.duration) return;
 
            // If we have a multi-field state, render all visible fields.
            // Otherwise fall back to the single-field currentMarkers list.
            const fieldIds = Object.keys(markerFieldsState);
            if (fieldIds.length > 0) {
                fieldIds.forEach(fid => {
                    const st = markerFieldsState[fid];
                    if (!st || !st.visible) return;
                    st.markers.forEach((m, idx) => drawMarkerBar(m, markersContainer, parseInt(fid), idx));
                });
            } else if (currentMarkers.length > 0) {
                currentMarkers.forEach((m, idx) => drawMarkerBar(m, markersContainer, currentMarkersFieldId, idx));
            }
        }
 
      // Helper: format seconds as HH:MM:SS.mmm for marker storage
        function secondsToHMS(sec) {
            sec = Math.max(0, sec);
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = Math.floor(sec % 60);
            const ms = Math.round((sec - Math.floor(sec)) * 1000);
            // Handle ms rounding to 1000 by rolling over the seconds
            let secOut = s, msOut = ms;
            if (msOut === 1000) { msOut = 0; secOut += 1; }
            return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(secOut).padStart(2,'0') + '.' + String(msOut).padStart(3,'0');
        }
 
        // Convert a clientX on the player progress container back to absolute seconds.
        function clientXToSeconds(clientX) {
            if (!audioPlayer.duration) return 0;
            const cont = document.getElementById('player-progress-container');
            const rect = cont.getBoundingClientRect();
            const screenFrac = (clientX - rect.left) / rect.width;
            const absFrac = waveformOffset + screenFrac / waveformZoom;
            return Math.max(0, Math.min(1, absFrac)) * audioPlayer.duration;
        }
 
        // Commit edited markers for a field back to the server.
        function commitMarkerField(fieldId, markers) {
            if (!fieldId || !markers) return;
            // Optimistic local update
            if (markerFieldsState[fieldId]) markerFieldsState[fieldId].markers = markers;
            if (currentMarkersFieldId === fieldId) currentMarkers = markers;
            const fileId = currentPlayingId || selectedFileId;
            if (!fileId) return;
            fetch('/api/file/' + fileId + '/value', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
            }).then(() => {
                // Refresh metadata panel so the right-side timecode displays update
                if (selectedFileId === fileId) loadFileMetadata(selectedFileId);
            });
        }

         function drawMarkerBar(m, container, fieldId, markerIndex) {
            const startSec = timeToSeconds(m.start);
            const endSec = timeToSeconds(m.end);
            // Convert absolute time to position within the zoomed, offset view.
            const startAbsFrac = startSec / audioPlayer.duration;
            const endAbsFrac = endSec / audioPlayer.duration;
            let startScreen = (startAbsFrac - waveformOffset) * waveformZoom * 100;
            let endScreen   = (endAbsFrac   - waveformOffset) * waveformZoom * 100;
            // Skip markers entirely outside the visible window
            if (endScreen < 0 || startScreen > 100) return;
            // Clip marker to visible area (so we draw a partial bar at edges)
            const clippedStart = Math.max(0, startScreen);
            const clippedEnd   = Math.min(100, endScreen);
            const width = Math.max(clippedEnd - clippedStart, 0.5);
 
            const color = getMarkerColor(m.color);
            const markerEl = document.createElement('div');
            markerEl.className = 'player-marker';
            markerEl.style.left = clippedStart + '%';
            markerEl.style.width = width + '%';
            markerEl.style.background = color.bg;
            // Only show left border if the marker's actual start is in view
            markerEl.style.borderLeftColor = (startScreen >= 0) ? color.border : 'transparent';
            markerEl.title = m.label + ' [' + m.start + ' - ' + m.end + ']\\nDrag edges to resize, drag body to move';
            // Click (without drag) seeks to marker start
            markerEl.onclick = (e) => {
                e.stopPropagation();
                if (markerEl.dataset.justDragged === '1') {
                    delete markerEl.dataset.justDragged;
                    return;
                }
                audioPlayer.currentTime = startSec;
            };
 
            const labelEl = document.createElement('span');
            labelEl.className = 'player-marker-label';
            labelEl.textContent = m.label;
            labelEl.style.color = color.text;
            labelEl.style.background = color.border;
            markerEl.appendChild(labelEl);
 
            // Edge handles + body drag — only when we know the field id
            if (fieldId != null && markerIndex != null) {
                attachMarkerDragHandles(markerEl, fieldId, markerIndex,
                                        startScreen >= 0, endScreen <= 100);
            }
 
            container.appendChild(markerEl);
        }
 
        // Attach left-edge, right-edge, and body drag handlers to a player marker.
        // showLeftHandle/showRightHandle: false if that edge is outside the visible view
        // (so dragging it would feel disconnected — we hide the grab zone in that case).
        function attachMarkerDragHandles(markerEl, fieldId, markerIndex, showLeftHandle, showRightHandle) {
            const HANDLE_PX = 6;
 
            // Left handle
            if (showLeftHandle) {
                const left = document.createElement('div');
                left.style.cssText =
                    'position:absolute;left:0;top:0;bottom:0;width:' + HANDLE_PX + 'px;' +
                    'cursor:ew-resize;background:transparent;z-index:6;';
                left.onmousedown = (e) => startMarkerDrag(e, fieldId, markerIndex, 'start');
                markerEl.appendChild(left);
            }
            // Right handle
            if (showRightHandle) {
                const right = document.createElement('div');
                right.style.cssText =
                    'position:absolute;right:0;top:0;bottom:0;width:' + HANDLE_PX + 'px;' +
                    'cursor:ew-resize;background:transparent;z-index:6;';
                right.onmousedown = (e) => startMarkerDrag(e, fieldId, markerIndex, 'end');
                markerEl.appendChild(right);
            }
            // Body drag (move whole marker) — uses the marker element itself but
            // mousedown only triggers a drag if the user actually moves the mouse.
            markerEl.addEventListener('mousedown', (e) => {
                // Skip if the mousedown landed on a handle
                if (e.target !== markerEl && e.target.tagName !== 'SPAN') return;
                startMarkerDrag(e, fieldId, markerIndex, 'move');
            });
        }
 
        // Active drag state — kept module-level so the move/up handlers find it
        let activeMarkerDrag = null;
 
        function startMarkerDrag(e, fieldId, markerIndex, mode) {
            e.preventDefault();
            e.stopPropagation();
            const st = markerFieldsState[fieldId];
            if (!st) return;
            const m = st.markers[markerIndex];
            if (!m) return;
            activeMarkerDrag = {
                fieldId, markerIndex, mode,
                startClientX: e.clientX,
                origStartSec: timeToSeconds(m.start),
                origEndSec: timeToSeconds(m.end),
                moved: false,
                markerEl: e.currentTarget.closest('.player-marker') || e.currentTarget,
            };
            document.body.style.cursor = mode === 'move' ? 'grabbing' : 'ew-resize';
            document.body.style.userSelect = 'none';
        }
 
        document.addEventListener('mousemove', (e) => {
            if (!activeMarkerDrag) return;
            const drag = activeMarkerDrag;
            const cont = document.getElementById('player-progress-container');
            const pxPerScreen = cont.offsetWidth;
            const dx = e.clientX - drag.startClientX;
            // Convert pixel delta to seconds, accounting for zoom
            const dSec = (dx / pxPerScreen) * (audioPlayer.duration / waveformZoom);
            if (Math.abs(dx) > 2) drag.moved = true;
            const dur = audioPlayer.duration || 0;
 
            const st = markerFieldsState[drag.fieldId];
            if (!st) return;
            const m = st.markers[drag.markerIndex];
            if (!m) return;
 
            if (drag.mode === 'start') {
                let newStart = Math.max(0, Math.min(drag.origEndSec - 0.05, drag.origStartSec + dSec));
                m.start = secondsToHMS(newStart);
            } else if (drag.mode === 'end') {
                let newEnd = Math.max(drag.origStartSec + 0.05, Math.min(dur, drag.origEndSec + dSec));
                m.end = secondsToHMS(newEnd);
            } else if (drag.mode === 'move') {
                const len = drag.origEndSec - drag.origStartSec;
                let newStart = Math.max(0, Math.min(dur - len, drag.origStartSec + dSec));
                m.start = secondsToHMS(newStart);
                m.end = secondsToHMS(newStart + len);
            }
            updatePlayerMarkers();
        });
 
        document.addEventListener('mouseup', () => {
            if (!activeMarkerDrag) return;
            const drag = activeMarkerDrag;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            if (drag.moved) {
                // Mark the marker so the next click handler ignores it (avoids snap-to-start)
                if (drag.markerEl) drag.markerEl.dataset.justDragged = '1';
                const st = markerFieldsState[drag.fieldId];
                if (st) commitMarkerField(drag.fieldId, st.markers);
            }
            activeMarkerDrag = null;
        });

 // ---- Waveform extras (zoom + marker filter) toggle ----
        function toggleWaveformExtras(forceState) {
            const extras = document.getElementById('waveform-extras');
            const toggle = document.getElementById('waveform-extras-toggle');
            if (!extras || !toggle) return;
            const isOpen = extras.style.display !== 'none';
            const willOpen = (typeof forceState === 'boolean') ? forceState : !isOpen;
            extras.style.display = willOpen ? 'flex' : 'none';
            toggle.textContent = willOpen ? '▾' : '▸';
            toggle.title = willOpen ? 'Hide zoom & marker controls' : 'Show zoom & marker controls';
            try { localStorage.setItem('earmark_waveform_extras_open', willOpen ? '1' : '0'); } catch(e) {}
        }
        // Restore saved state on load
        document.addEventListener('DOMContentLoaded', () => {
            try {
                const saved = localStorage.getItem('earmark_waveform_extras_open');
                if (saved === '1') toggleWaveformExtras(true);
            } catch(e) {}
        });
 
        // ---- Marker-line visibility controls ----
        function toggleMarkerVisibilityMenu(event) {
            if (event) event.stopPropagation();
            const menu = document.getElementById('marker-visibility-menu');
            if (!menu) return;
            const isVisible = menu.classList.contains('visible');
            if (!isVisible) renderMarkerVisibilityMenu();
            menu.classList.toggle('visible');
        }

        function renderMarkerVisibilityMenu() {
            const menu = document.getElementById('marker-visibility-menu');
            if (!menu) return;
            const ids = Object.keys(markerFieldsState);
            let html = '';
            if (ids.length === 0) {
                html = '<div style="padding:6px 8px;color:var(--grafite);font-size:12px;">No marker fields on this file</div>';
            } else {
                html += '<label><input type="checkbox" id="mv-all" ' +
                        (ids.every(id => markerFieldsState[id].visible) ? 'checked' : '') +
                        ' onchange="setAllMarkerVisibility(this.checked)"><span>Show all</span></label>';
                html += '<hr/>';
                ids.forEach(id => {
                    const st = markerFieldsState[id];
                    // Use first marker's color as a hint, or neutral
                    const first = st.markers[0];
                    const swatch = first ? getMarkerColor(first.color).border : 'var(--grafite)';
                    html += '<label>' +
                        '<input type="checkbox" ' + (st.visible ? 'checked' : '') +
                        ' onchange="setMarkerFieldVisibility(' + id + ', this.checked)">' +
                        '<span class="mv-swatch" style="background:' + swatch + '"></span>' +
                        '<span>' + escapeHtml(st.name) + '</span>' +
                        '<span style="margin-left:auto;color:var(--grafite);font-size:10px;">(' + st.markers.length + ')</span>' +
                    '</label>';
                });
                html += '<hr/>';
                html += '<label onclick="setAllMarkerVisibility(false); event.preventDefault();" style="justify-content:center;color:var(--grafite);">Hide all</label>';
            }
            menu.innerHTML = html;
        }

        function setMarkerFieldVisibility(fieldId, visible) {
            if (markerFieldsState[fieldId]) {
                markerFieldsState[fieldId].visible = visible;
            }
            updatePlayerMarkers();
            updateMarkerVisibilityLabel();
            // Refresh the "Show all" checkbox state
            renderMarkerVisibilityMenu();
        }

        function setAllMarkerVisibility(visible) {
            Object.keys(markerFieldsState).forEach(id => {
                markerFieldsState[id].visible = visible;
            });
            updatePlayerMarkers();
            updateMarkerVisibilityLabel();
            renderMarkerVisibilityMenu();
        }

        function updateMarkerVisibilityLabel() {
            const label = document.getElementById('marker-visibility-label');
            if (!label) return;
            const ids = Object.keys(markerFieldsState);
            if (ids.length === 0) { label.textContent = 'Markers'; return; }
            const vis = ids.filter(id => markerFieldsState[id].visible);
            if (vis.length === 0) label.textContent = 'Markers: none';
            else if (vis.length === ids.length) label.textContent = 'Markers: all';
            else if (vis.length === 1) label.textContent = markerFieldsState[vis[0]].name;
            else label.textContent = 'Markers: ' + vis.length + '/' + ids.length;
        }

        // Close the menu on outside click
        document.addEventListener('click', (e) => {
            const menu = document.getElementById('marker-visibility-menu');
            const btn  = document.getElementById('marker-visibility-btn');
            if (!menu || !btn) return;
            if (menu.classList.contains('visible') &&
                !menu.contains(e.target) && !btn.contains(e.target)) {
                menu.classList.remove('visible');
            }
        });

        function escapeHtml(s) {
            return String(s).replace(/[&<>"']/g, c => ({
                '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
            })[c]);
        }
        
        // Update markers when audio duration is loaded
        audioPlayer.addEventListener('loadedmetadata', () => {
            document.getElementById('player-duration').textContent = formatTimeHMS(audioPlayer.duration);
            console.log('Audio loaded, duration:', audioPlayer.duration);
            updatePlayerMarkers();
        });
        
        function dropMarkerAtPlayhead() {
            if (!currentPlayingId || !currentMarkersFieldId) {
                alert('Select a file with a markers field first, then play it.');
                return;
            }
            
            // Pause while adding marker
            const wasPlaying = !audioPlayer.paused;
            audioPlayer.pause();
            
            const currentTime = formatTime(audioPlayer.currentTime);
            qdPrompt('Label for the marker at ' + currentTime + ':', '', {title: 'New marker', ok: 'Add'}).then(label => {
            if (!label) {
                if (wasPlaying) audioPlayer.play();
                return;
            }
            
            // Default color is Yellow
            const color = 'Yellow';
            
            // Default end time is current time + 2 seconds
            const endTime = formatTime(Math.min(audioPlayer.currentTime + 2, audioPlayer.duration || audioPlayer.currentTime + 2));
            
            // Add marker
            fetch('/api/file/' + currentPlayingId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === currentMarkersFieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    markers.push({ start: currentTime, end: endTime, label: label.trim(), color: color });
                    
                    fetch('/api/file/' + currentPlayingId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: currentMarkersFieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        currentMarkers = markers;
                        if (markerFieldsState[currentMarkersFieldId]) markerFieldsState[currentMarkersFieldId].markers = markers;
                        updatePlayerMarkers();
                        if (selectedFileId === currentPlayingId) {
                            loadFileMetadata(selectedFileId);
                        }
                        if (wasPlaying) audioPlayer.play();
                    });
                });
            });
        }
        
        function jumpToNextMarker() {
            if (currentMarkers.length === 0) return;
            
            const currentTime = audioPlayer.currentTime;
            const sorted = [...currentMarkers].sort((a, b) => timeToSeconds(a.start) - timeToSeconds(b.start));
            
            for (const m of sorted) {
                const markerTime = timeToSeconds(m.start);
                if (markerTime > currentTime + 0.5) {
                    audioPlayer.currentTime = markerTime;
                    return;
                }
            }
            // Wrap to first marker
            audioPlayer.currentTime = timeToSeconds(sorted[0].start);
        }
        
        function jumpToPreviousMarker() {
            if (currentMarkers.length === 0) return;
            
            const currentTime = audioPlayer.currentTime;
            const sorted = [...currentMarkers].sort((a, b) => timeToSeconds(b.start) - timeToSeconds(a.start));
            
            for (const m of sorted) {
                const markerTime = timeToSeconds(m.start);
                if (markerTime < currentTime - 0.5) {
                    audioPlayer.currentTime = markerTime;
                    return;
                }
            }
            // Wrap to last marker
            audioPlayer.currentTime = timeToSeconds(sorted[0].start);
        }
        
        function jumpToMarker(startTime) {
            audioPlayer.currentTime = timeToSeconds(startTime);
            if (audioPlayer.paused) {
                audioPlayer.play();
            }
        }
        
        function loadFileMetadata(fileId) {
            fetch('/api/file/' + fileId)
                .then(r => r.json())
                .then(file => {
                    document.getElementById('selected-file-name').textContent = file.filename;

                    // Bulk-edit banner: when more than one file is selected, edits in
                    // this panel apply to all of them (except markers/datalink, which
                    // remain single-file).
                    const _bulkBanner = isBulkActive()
                        ? `<div style="margin: 0 0 12px 0; padding: 8px 10px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.4); border-radius: 4px; font-size: 12px; color: var(--ardesia);">
                               Editing <b>${selectedFileIds.size}</b> files — changes apply to all selected.
                               <span style="color:var(--grafite);">(Markers and datalinks affect only the focused file.)</span>
                           </div>`
                        : '';

                    let html = `
                        ${_bulkBanner}
                        <div style="margin-bottom: 10px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center;">
                            <button class="btn btn-small btn-secondary" onclick="importFileMetadata(false)"
                                    title="Read embedded metadata from the audio file (BWF, iXML, ID3, FLAC, MP4 tags) and the file's modification date. Existing values are preserved.">
                                Import file metadata
                            </button>
                            <button class="btn btn-small btn-secondary" onclick="importFileMetadata(true)"
                                    title="Same as Import file metadata, but overwrites any existing values for these fields.">
                                Overwrite from file
                            </button>
                            <select onchange="moveSelectionToGroup(this.value === '' ? null : parseInt(this.value, 10)); this.value='';"
                                    title="Move the focused file (or whole selection) into a group"
                                    style="font-size:12px; padding:3px 4px;">
                                <option value="">Move to group…</option>
                                <option value="">— Ungrouped —</option>
                                ${(typeof allGroups !== 'undefined' && allGroups.length)
                                    ? _renderGroupSelectOptions()
                                    : ''}
                            </select>
                        </div>
                        <div class="form-group">
                            <label>Tags</label>
                            <div class="tags-display" id="tags-display">
                                ${file.tags.map((t, i) => `<span class="tag tag-removable">${t} <span style="cursor:pointer;" onclick="removeTag('${t.replace(/'/g, "\\'")}')">×</span></span>`).join('')}
                            </div>
                            <div class="tag-input-group">
                                <input type="text" id="new-tag" placeholder="Add tag..." onkeypress="if(event.key==='Enter')addTag()">
                                <button class="btn" onclick="addTag()">Add</button>
                            </div>
                        </div>
                    `;
                    
                    // Add fields
                    fetch('/api/fields')
                        .then(r => r.json())
                        .then(fields => {
                            // Preserve existing visibility choices across re-renders, then
                            // rebuild markerFieldsState from scratch for this file.
                            const prevVisibility = {};
                            Object.keys(markerFieldsState).forEach(id => {
                                prevVisibility[id] = markerFieldsState[id].visible;
                            });
                            markerFieldsState = {};

                            fields.forEach(field => {
                                const value = file.fields[field.name] || '';
                                const hasValue = value !== '';
                                const inactiveClass = hasValue ? '' : 'inactive';
                                
                                if (field.type === 'parameter') {
                                    html += `
                                        <div class="form-group ${inactiveClass}">
                                            <label>${field.name}</label>
                                            <input type="range" min="${field.min}" max="${field.max}" value="${value || field.min}" 
                                                onchange="setValue(${field.id}, this.value); this.parentElement.classList.remove('inactive');" 
                                                oninput="this.nextElementSibling.textContent = 'Value: ' + this.value">
                                            <div class="value-display">Value: ${value || field.min}</div>
                                        </div>
                                    `;
                                } else if (field.type === 'text') {
                                    html += `
                                        <div class="form-group ${inactiveClass}">
                                            <label>${field.name}</label>
                                            <textarea onchange="setValue(${field.id}, this.value); this.parentElement.classList.remove('inactive');" 
                                                onfocus="this.parentElement.classList.remove('inactive');"
                                                placeholder="Enter text...">${value}</textarea>
                                        </div>
                                    `;
                                } else if (field.type === 'choice') {
                                    html += `
                                        <div class="form-group ${inactiveClass}">
                                            <label>${field.name}</label>
                                            <select onchange="setValue(${field.id}, this.value); if(this.value) this.parentElement.classList.remove('inactive');">
                                                <option value="">Select...</option>
                                                ${field.choices.map(c => `<option value="${c}" ${value === c ? 'selected' : ''}>${c}</option>`).join('')}
                                            </select>
                                        </div>
                                    `;
                                } else if (field.type === 'datalink') {
                                    const hasContent = file.fields[field.name + '_content'];
                                    html += `
                                        <div class="form-group ${inactiveClass}">
                                            <label>${field.name} ${hasContent ? '<span style="color: var(--ok); font-size: 10px;">● indexed</span>' : ''}</label>
                                            ${value ? `
                                                <div class="datalink-display">
                                                    <a href="#" onclick="openDataLink('${value.replace(/'/g, "\\'")}'); return false;" title="${value}">${value.split('/').pop()}</a>
                                                    <div style="float: right;">
                                                        <button class="btn btn-small btn-secondary" style="padding: 2px 6px; margin-right: 4px;" 
                                                            onclick="reindexDataLink(${field.id})" title="Re-index file content">↻</button>
                                                        <button class="btn btn-small btn-danger" style="padding: 2px 6px;" 
                                                            onclick="clearDataLink(${field.id})">×</button>
                                                    </div>
                                                </div>
                                                ${hasContent ? `<div style="font-size: 11px; color: var(--grafite); margin-top: 4px;">Content searchable (${Math.round(file.fields[field.name + '_content'].length / 1000)}KB indexed)</div>` : 
                                                    `<div style="font-size: 11px; color: var(--warn); margin-top: 4px;">Warning: content not indexed - file may not exist or be unreadable</div>`}
                                            ` : ''}
                                            <div class="tag-input-group" style="margin-top: 8px;">
                                                <input type="text" id="datalink-${field.id}" placeholder="Enter file path..." value="${value}">
                                                <button class="btn btn-small" onclick="setDataLink(${field.id})">Set</button>
                                            </div>
                                        </div>
                                    `;
                                } else if (field.type === 'markers') {
                                    let markers = [];
                                    try {
                                        markers = value ? JSON.parse(value) : [];
                                    } catch(e) { markers = []; }

                                    // Sort markers by start time
                                    markers.sort((a, b) => timeToSeconds(a.start) - timeToSeconds(b.start));

                                    // Register this field in the multi-field state (preserve
                                    // visibility choice across re-renders; default to visible)
                                    markerFieldsState[field.id] = {
                                        name: field.name,
                                        markers: markers,
                                        visible: (field.id in prevVisibility) ? prevVisibility[field.id] : true
                                    };

                                    // Keep legacy single-field pointer pointing at the *last*
                                    // markers field seen, so M-key drops and prev/next still work.
                                    currentMarkers = markers;
                                    currentMarkersFieldId = field.id;

                                    // Update player markers if playing
                                    if (currentPlayingId === selectedFileId) {
                                        setTimeout(updatePlayerMarkers, 100);
                                    }
                                    
                                    html += `
                                        <div class="form-group">
                                            <label>${field.name}</label>
                                            <div class="markers-list" id="markers-${field.id}">
                                                ${markers.map((m, i) => {
                                                    const color = markerColors.find(c => c.name === m.color) || markerColors[0];
                                                    return `
                                                     <div class="marker-item" style="cursor: pointer;">
                                                        <div class="marker-color-bar" style="background: ${color.border};" ondblclick="event.stopPropagation(); showColorPicker(${field.id}, ${i}, this)" title="Double-click to change color"></div>
                                                        <span class="marker-time" onmousedown="startTimecodeScrub(event, ${field.id}, ${i}, 'start')" title="Drag horizontally to scrub, click to type">${m.start}</span>
                                                        <span class="marker-time-separator">-</span>
                                                        <span class="marker-time" onmousedown="startTimecodeScrub(event, ${field.id}, ${i}, 'end')" title="Drag horizontally to scrub, click to type">${m.end}</span>
                                                        <span class="marker-label" onclick="event.stopPropagation(); editMarkerLabel(${field.id}, ${i})" title="Click to edit">${m.label}</span>
                                                        <button class="marker-play-btn" onclick="event.stopPropagation(); jumpToMarker('${m.start}')" title="Play from here">▶</button>
                                                        <button class="marker-remove" onclick="event.stopPropagation(); removeMarker(${field.id}, ${i})">×</button>
                                                    </div>
                                                `}).join('')}
                                                ${markers.length === 0 ? '<div style="color: var(--grafite); font-size: 13px; padding: 8px;">No markers yet (press M while playing to add)</div>' : ''}
                                            </div>
                                            <div class="marker-input-row">
                                                <input type="text" class="time-input" id="marker-start-${field.id}" placeholder="HH:MM:SS.mmm" value="00:00:00.000">
                                                <input type="text" class="time-input" id="marker-end-${field.id}" placeholder="HH:MM:SS.mmm" value="00:00:00.000">
                                            </div>
                                            <div class="marker-input-row">
                                                <input type="text" id="marker-label-${field.id}" placeholder="Label...">
                                                <button class="btn btn-small" onclick="addMarker(${field.id})">+ Add</button>
                                            </div>
                                        </div>
                                    `;
                                } else if (field.type === 'date') {
                                    html += `
                                        <div class="form-group ${inactiveClass}">
                                            <label>${field.name}</label>
                                            <input type="date" value="${value}"
                                                onchange="setValue(${field.id}, this.value); if(this.value) this.parentElement.classList.remove('inactive');">
                                        </div>
                                    `;
                                } else if (field.type === 'tags') {
                                    // Field-specific tags
                                    let fieldTags = [];
                                    try {
                                        fieldTags = value ? JSON.parse(value) : [];
                                    } catch(e) { fieldTags = []; }
                                    
                                    html += `
                                        <div class="form-group">
                                            <label>${field.name}</label>
                                            <div class="tags-display" id="field-tags-${field.id}">
                                                ${fieldTags.map(t => `<span class="tag tag-removable">${t} <span style="cursor:pointer;" onclick="removeFieldTag(${field.id}, '${t.replace(/'/g, "\\'")}')">×</span></span>`).join('')}
                                            </div>
                                            <div class="tag-input-group">
                                                <input type="text" id="field-tag-input-${field.id}" placeholder="Add tag..." 
                                                    onkeypress="if(event.key==='Enter')addFieldTag(${field.id})">
                                                <button class="btn btn-small" onclick="addFieldTag(${field.id})">Add</button>
                                            </div>
                                        </div>
                                    `;
                                }
                            });

                            document.getElementById('metadata-panel').innerHTML = html;

                            // Refresh the marker-visibility dropdown to reflect whatever
                            // marker fields exist on this file, and redraw the player markers.
                            updateMarkerVisibilityLabel();
                            updatePlayerMarkers();
                        });
                });
        }
        
        // Time helper functions
        function timeToSeconds(timeStr) {
            if (!timeStr) return 0;
            const parts = timeStr.split(':').map(Number);
            if (parts.length === 3) {
                return parts[0] * 3600 + parts[1] * 60 + parts[2];
            } else if (parts.length === 2) {
                return parts[0] * 60 + parts[1];
            } else if (parts.length === 1) {
                return parts[0];
            }
            return 0;
        }
        
        // Marker colors
        const markerColors = [
            {name: 'Yellow', bg: 'rgba(192, 138, 46, 0.30)', border: '#c08a2e', text: '#1a1c1d'},
            {name: 'Blue', bg: 'rgba(74, 115, 145, 0.30)', border: '#4a7391', text: '#f3f4f2'},
            {name: 'Green', bg: 'rgba(95, 125, 78, 0.30)', border: '#5f7d4e', text: '#f3f4f2'},
            {name: 'Red', bg: 'rgba(184, 69, 47, 0.30)', border: '#b8452f', text: '#f3f4f2'},
            {name: 'Purple', bg: 'rgba(122, 90, 140, 0.30)', border: '#7a5a8c', text: '#f3f4f2'},
            {name: 'Pink', bg: 'rgba(181, 97, 125, 0.30)', border: '#b5617d', text: '#f3f4f2'},
            {name: 'Orange', bg: 'rgba(196, 105, 42, 0.30)', border: '#c4692a', text: '#1a1c1d'},
            {name: 'Cyan', bg: 'rgba(63, 138, 140, 0.30)', border: '#3f8a8c', text: '#f3f4f2'},
        ];
        
        function getMarkerColor(colorName) {
            return markerColors.find(c => c.name === colorName) || markerColors[0];
        }
        
       function formatTime(seconds) {
            // Delegate to secondsToHMS so all marker timecodes share the same HH:MM:SS.mmm format
            return secondsToHMS(seconds);
        }
        
        // Marker functions
        function addMarker(fieldId) {
            const start = document.getElementById(`marker-start-${fieldId}`).value;
            const end = document.getElementById(`marker-end-${fieldId}`).value;
            const label = document.getElementById(`marker-label-${fieldId}`).value;
            
            if (!label.trim()) {
                alert('Please enter a label for the marker');
                return;
            }
            
            // Validate time format — milliseconds (.mmm) optional
            const timeRegex = /^\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,3})?$/;
            if (!timeRegex.test(start) || !timeRegex.test(end)) {
                alert('Please use HH:MM:SS or HH:MM:SS.mmm format for times');
                return;
            }
            
            fetch('/api/file/' + selectedFileId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === fieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    markers.push({ start, end, label: label.trim() });
                    
                    fetch('/api/file/' + selectedFileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        loadFileMetadata(selectedFileId);
                    });
                });
        }
        
        function removeMarker(fieldId, markerIndex) {
            fetch('/api/file/' + selectedFileId)
                .then(r => r.json())
                .then(file => {
                    const field = allFields.find(f => f.id === fieldId);
                    let markers = [];
                    try {
                        markers = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { markers = []; }
                    
                    // Sort first (same as display)
                    markers.sort((a, b) => timeToSeconds(a.start) - timeToSeconds(b.start));
                    markers.splice(markerIndex, 1);
                    
                    fetch('/api/file/' + selectedFileId + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(markers)})
                    }).then(() => {
                        loadFileMetadata(selectedFileId);
                    });
                });
        }
        
        // Data link functions
        function setDataLink(fieldId) {
            const path = document.getElementById(`datalink-${fieldId}`).value.trim();
            if (!path) return;
            
            fetch('/api/file/' + selectedFileId + '/value', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field_id: fieldId, value: path})
            }).then(() => {
                loadFileMetadata(selectedFileId);
            });
        }
        
        function clearDataLink(fieldId) {
            fetch('/api/file/' + selectedFileId + '/value', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field_id: fieldId, value: ''})
            }).then(() => {
                loadFileMetadata(selectedFileId);
            });
        }
        
        function openDataLink(path) {
            const panel = document.getElementById('linked-file-panel');
            const titleEl = document.getElementById('linked-file-title');
            const pathEl  = document.getElementById('linked-file-path');
            const bodyEl  = document.getElementById('linked-file-content');
            if (!panel || !titleEl || !pathEl || !bodyEl) {
                alert('Opening: ' + path);
                return;
            }

            titleEl.textContent = path.split(/[\\\\/]/).pop() || path;
            pathEl.textContent = path;
            bodyEl.className = 'linked-file-panel-content loading';
            bodyEl.textContent = 'Loading…';
            panel.style.display = 'flex';
            // Cascade slightly each time the panel reopens so multiple opens
            // don't all stack at the exact same position.
            const offset = (window.__linkedFileOpenCount = (window.__linkedFileOpenCount || 0) + 1) * 20;
            if (!panel.dataset.userMoved) {
                panel.style.top  = (100 + (offset % 80)) + 'px';
                panel.style.left = (200 + (offset % 80)) + 'px';
            }

            fetch('/api/datalink/content?path=' + encodeURIComponent(path))
                .then(r => r.json())
                .then(data => {
                    if (data.ok) {
                        bodyEl.className = 'linked-file-panel-content';
                        bodyEl.textContent = data.content || '(empty file)';
                        if (data.truncated) {
                            const note = document.createElement('div');
                            note.style.cssText = 'color:var(--warn);font-size:11px;margin-top:12px;padding-top:8px;border-top:1px solid var(--rule-soft);';
                            note.textContent = 'Warning: file truncated at ' + Math.round(data.size_limit / 1024) + 'KB. Full size: ' + Math.round(data.full_size / 1024) + 'KB.';
                            bodyEl.appendChild(note);
                        }
                    } else {
                        bodyEl.className = 'linked-file-panel-content error';
                        bodyEl.textContent = data.error || 'Could not read file.';
                    }
                })
                .catch(err => {
                    bodyEl.className = 'linked-file-panel-content error';
                    bodyEl.textContent = 'Request failed: ' + err.message;
                });
        }

        function closeLinkedFile() {
            const panel = document.getElementById('linked-file-panel');
            if (panel) panel.style.display = 'none';
        }

        // Drag the linked-file panel by its header
        document.addEventListener('DOMContentLoaded', () => {
            const panel = document.getElementById('linked-file-panel');
            const handle = document.getElementById('linked-file-drag-handle');
            const resize = document.getElementById('linked-file-resize');
            if (!panel || !handle || !resize) return;

            // --- Drag move ---
            let dragState = null;
            handle.addEventListener('mousedown', (e) => {
                // Don't start a drag when the user clicks the close button
                if (e.target.closest('.linked-file-panel-close')) return;
                const rect = panel.getBoundingClientRect();
                dragState = {
                    startX: e.clientX,
                    startY: e.clientY,
                    origLeft: rect.left,
                    origTop: rect.top,
                };
                panel.dataset.userMoved = '1';
                e.preventDefault();
            });
            document.addEventListener('mousemove', (e) => {
                if (!dragState) return;
                const dx = e.clientX - dragState.startX;
                const dy = e.clientY - dragState.startY;
                // Clamp to viewport so the panel can't be dragged completely offscreen
                const newLeft = Math.max(-panel.offsetWidth + 80, Math.min(window.innerWidth - 80, dragState.origLeft + dx));
                const newTop  = Math.max(0, Math.min(window.innerHeight - 40, dragState.origTop + dy));
                panel.style.left = newLeft + 'px';
                panel.style.top  = newTop + 'px';
            });
            document.addEventListener('mouseup', () => { dragState = null; });

            // --- Resize from bottom-right corner ---
            let resizeState = null;
            resize.addEventListener('mousedown', (e) => {
                resizeState = {
                    startX: e.clientX,
                    startY: e.clientY,
                    origW: panel.offsetWidth,
                    origH: panel.offsetHeight,
                };
                e.preventDefault();
                e.stopPropagation();
            });
            document.addEventListener('mousemove', (e) => {
                if (!resizeState) return;
                const dx = e.clientX - resizeState.startX;
                const dy = e.clientY - resizeState.startY;
                panel.style.width  = Math.max(300, resizeState.origW + dx) + 'px';
                panel.style.height = Math.max(200, resizeState.origH + dy) + 'px';
            });
            document.addEventListener('mouseup', () => { resizeState = null; });
        });
        
        function reindexDataLink(fieldId) {
            // Re-set the same value to trigger re-indexing
            const input = document.getElementById(`datalink-${fieldId}`);
            const path = input.value.trim();
            if (!path) return;
            
            fetch('/api/file/' + selectedFileId + '/value', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field_id: fieldId, value: path})
            }).then(r => r.json()).then(result => {
                if (result.indexed) {
                    loadFileMetadata(selectedFileId);
                } else {
                    alert('Could not index file. Make sure the path is correct and the file is readable.');
                }
            });
        }
        
        // Field-specific tag functions
        // When multiple files are selected, the tag is added to / removed from each
        // file's own tag set (not by overwriting them with a shared array).
        function _mutateFieldTagsForIds(ids, fieldId, mutator) {
            const field = allFields.find(f => f.id === fieldId);
            if (!field) return Promise.resolve();
            return Promise.all(ids.map(id =>
                fetch('/api/file/' + id).then(r => r.json()).then(file => {
                    let tags = [];
                    try {
                        tags = file.fields[field.name] ? JSON.parse(file.fields[field.name]) : [];
                    } catch(e) { tags = []; }
                    tags = mutator(tags);
                    return fetch('/api/file/' + id + '/value', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({field_id: fieldId, value: JSON.stringify(tags)})
                    });
                })
            ));
        }

        function addFieldTag(fieldId) {
            const input = document.getElementById(`field-tag-input-${fieldId}`);
            const tag = input.value.trim();
            if (!tag) return;
            const ids = getActiveTargetIds();
            if (ids.length === 0) return;
            _mutateFieldTagsForIds(ids, fieldId, tags => {
                if (!tags.includes(tag)) tags.push(tag);
                return tags;
            }).then(() => {
                input.value = '';
                if (isBulkActive()) loadFiles();
                if (selectedFileId != null) loadFileMetadata(selectedFileId);
            });
        }

        function removeFieldTag(fieldId, tag) {
            const ids = getActiveTargetIds();
            if (ids.length === 0) return;
            _mutateFieldTagsForIds(ids, fieldId, tags => tags.filter(t => t !== tag))
                .then(() => {
                    if (isBulkActive()) loadFiles();
                    if (selectedFileId != null) loadFileMetadata(selectedFileId);
                });
        }
        
        // Batch operations
        function batchAddTag() {
            qdPrompt('Tag to add to the ' + selectedFileIds.size + ' selected file(s):', '', {title: 'Batch tag', ok: 'Add'}).then(tag => {
            if (!tag) return;
            
            fetch('/api/files/batch/tag', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_ids: Array.from(selectedFileIds), tag})
            }).then(() => {
                loadFiles();
                if (selectedFileId) loadFileMetadata(selectedFileId);
            });
            });
        }
        
        function batchSetField() {
            if (allFields.length === 0) {
                alert('No fields defined. Create a field first.');
                return;
            }
            
            qdDialog({
                title: 'Set field',
                message: 'Set one field to the same value on the ' + selectedFileIds.size + ' selected file(s).',
                fields: [
                    {name: 'field', label: 'Field', type: 'select', options: allFields.map(f => ({value: String(f.id), label: f.name + '  (' + f.type + ')'}))},
                    {name: 'value', label: 'Value', type: 'text'}
                ],
                ok: 'Set'
            }).then(v => {
                if (!v) return;
                const field = allFields.find(f => String(f.id) === v.field);
                if (!field) return;
                fetch('/api/files/batch/value', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_ids: Array.from(selectedFileIds), field_id: field.id, value: v.value})
                }).then(() => {
                    loadFiles();
                    if (selectedFileId) loadFileMetadata(selectedFileId);
                });
            });
        }
        
        function batchDelete() {
            const count = selectedFileIds.size;
            qdConfirm(`Remove ${count} file(s) from the database? The audio files on disk are not touched.`, {title: 'Remove files', ok: 'Remove', danger: true}).then(ok => { if (!ok) return;
            
            fetch('/api/files/batch/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_ids: Array.from(selectedFileIds)})
            }).then(() => {
                if (selectedFileIds.has(selectedFileId)) {
                    selectedFileId = null;
                    document.getElementById('selected-file-name').textContent = 'No file selected';
                    document.getElementById('metadata-panel').innerHTML = '<div class="empty-state">Select a file to edit metadata</div>';
                }
                selectedFileIds.clear();
                loadFiles();
            });
            });
        }
        
        // ---- Bulk edit helpers --------------------------------------------------
        // When more than one file is selected, metadata-panel edits fan out to all
        // of them. When only one (or none) is selected, behavior is identical to
        // the previous single-file flow.
        function isBulkActive() {
            return selectedFileIds && selectedFileIds.size > 1;
        }
        function getActiveTargetIds() {
            if (isBulkActive()) return Array.from(selectedFileIds);
            if (selectedFileId != null) return [selectedFileId];
            return [];
        }
        function postFieldValue(fieldId, value) {
            const ids = getActiveTargetIds();
            if (ids.length === 0) return Promise.resolve();
            if (ids.length > 1) {
                return fetch('/api/files/batch/value', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_ids: ids, field_id: fieldId, value})
                });
            }
            return fetch('/api/file/' + ids[0] + '/value', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({field_id: fieldId, value})
            });
        }
        function postAddTag(tag) {
            const ids = getActiveTargetIds();
            if (ids.length === 0) return Promise.resolve();
            if (ids.length > 1) {
                return fetch('/api/files/batch/tag', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({file_ids: ids, tag})
                });
            }
            return fetch('/api/file/' + ids[0] + '/tag', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tag})
            });
        }
        // -------------------------------------------------------------------------

        function addTag() {
            const input = document.getElementById('new-tag');
            const tag = input.value.trim();
            if (!tag) return;
            const wasBulk = isBulkActive();
            postAddTag(tag).then(() => {
                // Undo is single-file only for now; skip when fanning out.
                if (!wasBulk && selectedFileId != null) {
                    pushUndo({type: 'addTag', fileId: selectedFileId, tag: tag});
                }
                input.value = '';
                loadFiles();
                if (selectedFileId != null) loadFileMetadata(selectedFileId);
            });
        }

        function removeTag(tag) {
            // Built-in tag removal stays single-file (no batch endpoint exists yet).
            // Operates on the focused file even when a multi-selection is active.
            if (selectedFileId == null) return;
            pushUndo({type: 'removeTag', fileId: selectedFileId, tag: tag});
            fetch('/api/file/' + selectedFileId + '/tag', {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tag})
            }).then(() => {
                loadFiles();
                loadFileMetadata(selectedFileId);
            });
        }

        function setValue(fieldId, value, oldValue) {
            const wasBulk = isBulkActive();
            // Undo currently records only single-file changes; bulk edits are not undoable.
            if (oldValue !== undefined && !wasBulk && selectedFileId != null) {
                pushUndo({type: 'setValue', fileId: selectedFileId, fieldId: fieldId, oldValue: oldValue, newValue: value});
            }
            postFieldValue(fieldId, value).then(() => {
                if (wasBulk) loadFiles();
            });
        }
        
        // Import metadata embedded in the audio files themselves (BWF/iXML/ID3/etc.)
        // for the focused file or, in bulk mode, for every selected file.
        function importFileMetadata(overwrite) {
            const ids = getActiveTargetIds();
            if (ids.length === 0) {
                alert('No file selected.');
                return;
            }
            const verb = overwrite ? 'overwrite values from' : 'import metadata from';
            const ask = ids.length > 1
                ? qdConfirm(`This will ${verb} ${ids.length} file(s). New fields are created automatically if needed.`, {title: 'Import file metadata', ok: 'Continue'})
                : Promise.resolve(true);
            ask.then(ok => {
            if (!ok) return;
            fetch('/api/files/import-metadata', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({file_ids: ids, overwrite: !!overwrite})
            }).then(r => r.json()).then(res => {
                if (!res.success) {
                    alert('Import failed: ' + (res.error || 'unknown error'));
                    return;
                }
                const parts = [
                    `Files processed: ${res.files_processed}`,
                    `New fields created: ${res.fields_created}`,
                    `Values written: ${res.values_set}`,
                ];
                if (res.values_skipped_existing) parts.push(`Values skipped (already set): ${res.values_skipped_existing}`);
                if (res.files_skipped_missing) parts.push(`Files skipped (path missing): ${res.files_skipped_missing}`);
                console.log('[import-metadata] ' + parts.join(' | '));
                loadFields();
                loadFiles();
                if (selectedFileId != null) loadFileMetadata(selectedFileId);
            }).catch(err => {
                alert('Import failed: ' + err);
            });
            });
        }

        function importFiles() {
            qdDialog({
                title: 'Import audio',
                fields: [
                    {name: 'path', label: nativeDialogs ? 'Folder or audio files' : 'Folder with audio files', type: 'text', mono: true, placeholder: '/Users/…/Recordings',
                     browse: [{kind: 'folder', label: 'Folder…'}, {kind: 'files', label: 'Files…'}]},
                    {name: 'recursive', label: 'Include subfolders (when importing a folder)', type: 'check', value: true}
                ],
                ok: 'Import',
                validate: v => (v.pathPaths || v.path.trim()) ? null : (nativeDialogs ? 'Choose a folder or files, or type a folder path.' : 'Enter a folder path.')
            }).then(v => {
                if (!v) return;
                const body = v.pathPaths ? {paths: v.pathPaths} : {path: v.path.trim(), recursive: v.recursive};
                fetch('/api/import', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                }).then(r => r.json()).then(result => {
                    let msg = `Imported ${result.imported} file(s)`;
                    if (result.skipped) msg += ` · ${result.skipped} already in the database`;
                    qdNotice(msg);
                    loadFiles();
                });
            });
        }
        
        function linkMetadataFiles() {
            qdDialog({
                title: 'Link metadata files',
                message: 'Files are matched to audio by name: rec_001.wav ↔ rec_001.txt, rec_001_pitch.csv …',
                fields: [
                    {name: 'path', label: 'Folder with metadata files (.txt, .TextGrid, .csv …)', type: 'text', mono: true, placeholder: '/Users/…/Transcriptions',
                     browse: [{kind: 'folder', label: 'Folder…'}]},
                    {name: 'fieldName', label: 'Field name', type: 'text', placeholder: 'Transcription, Formants, PitchTrack …'},
                    {name: 'recursive', label: 'Include subfolders', type: 'check', value: true}
                ],
                ok: 'Link',
                validate: v => !v.path.trim() ? 'Enter a folder path.' : (!v.fieldName.trim() ? 'Enter a field name.' : null)
            }).then(v => {
            if (!v) return;
            const path = v.path.trim(), fieldName = v.fieldName.trim(), recursive = v.recursive;
            fetch('/api/link-metadata', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path, fieldName, recursive})
            }).then(r => r.json()).then(result => {
                let msg = `Linked ${result.linked} file(s) to field "${fieldName}"`;
                if (result.notFound > 0) {
                    msg += `\n${result.notFound} file(s) had no matching audio`;
                }
                qdNotice(msg);
                loadFields();
                if (selectedFileId) loadFileMetadata(selectedFileId);
            });
            });
        }
        
        // ---- New field: a Quadro dialog instead of a chain of prompts ----
        var nfType = null;
        function newField() { openFieldDialog(); }
        function openFieldDialog() {
            nfType = null;
            document.getElementById('nf-name').value = '';
            document.getElementById('nf-min').value = '0';
            document.getElementById('nf-max').value = '10';
            document.getElementById('nf-choices').value = '';
            setFieldDialogError('');
            renderFieldTypes();
            document.getElementById('field-modal').classList.add('visible');
            setTimeout(function () { document.getElementById('nf-name').focus(); }, 0);
        }
        function closeFieldDialog() {
            document.getElementById('field-modal').classList.remove('visible');
        }
        function setFieldDialogError(t) {
            var el = document.getElementById('nf-error');
            el.textContent = t;
            el.style.display = t ? 'block' : 'none';
        }
        function renderFieldTypes() {
            var btns = document.querySelectorAll('#nf-types .nf-type');
            for (var i = 0; i < btns.length; i++) {
                btns[i].classList.toggle('active', btns[i].getAttribute('data-type') === nfType);
            }
            document.getElementById('nf-opts-parameter').style.display = nfType === 'parameter' ? 'block' : 'none';
            document.getElementById('nf-opts-choice').style.display = nfType === 'choice' ? 'block' : 'none';
        }
        (function () {
            var btns = document.querySelectorAll('#nf-types .nf-type');
            for (var i = 0; i < btns.length; i++) {
                btns[i].addEventListener('click', function () {
                    nfType = this.getAttribute('data-type');
                    setFieldDialogError('');
                    renderFieldTypes();
                });
            }
            // Keys typed in the dialog stay in the dialog (no app shortcuts).
            document.getElementById('field-modal').addEventListener('keydown', function (e) {
                if (e.key === 'Escape') { e.preventDefault(); closeFieldDialog(); }
                else if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') { e.preventDefault(); submitFieldDialog(); }
                e.stopPropagation();
            });
        })();
        function submitFieldDialog() {
            var name = document.getElementById('nf-name').value.trim();
            if (!name) { setFieldDialogError('Give the field a name.'); document.getElementById('nf-name').focus(); return; }
            if (!nfType) { setFieldDialogError('Choose a type.'); return; }
            if (allFields.some(function (f) { return f.name.toLowerCase() === name.toLowerCase(); })) {
                setFieldDialogError('There is already a field called "' + name + '".');
                return;
            }
            var min_val = null, max_val = null, choices = null;
            if (nfType === 'parameter') {
                min_val = parseFloat(document.getElementById('nf-min').value);
                max_val = parseFloat(document.getElementById('nf-max').value);
                if (isNaN(min_val) || isNaN(max_val)) { setFieldDialogError('Min and max must be numbers.'); return; }
                if (min_val >= max_val) { setFieldDialogError('Max must be larger than min.'); return; }
            } else if (nfType === 'choice') {
                choices = document.getElementById('nf-choices').value.split(/[\\n,]/).map(function (c) { return c.trim(); })
                    .filter(function (c, i, arr) { return c && arr.indexOf(c) === i; });
                if (!choices.length) { setFieldDialogError('Add at least one option.'); return; }
            }
            fetch('/api/field', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name: name, type: nfType, min_val: min_val, max_val: max_val, choices: choices })
            }).then(function (r) { return r.json(); }).then(function (res) {
                if (!res || res.id == null) { setFieldDialogError('Could not create the field (is the name already used?).'); return; }
                closeFieldDialog();
                loadFields();
                if (selectedFileId) loadFileMetadata(selectedFileId);
            }).catch(function (err) { setFieldDialogError('Could not create the field: ' + err.message); });
        }

        function showConsole() {
            var panel = document.getElementById('console-panel');
            if (panel.style.display === 'none') {
                panel.style.display = 'block';
                document.getElementById('console-input').focus();
            } else {
                panel.style.display = 'none';
            }
        }
        
        var consoleCmdHistory = [];
        var consoleCmdIndex = -1;
        
        function consoleKeyDown(e) {
            if (e.key === 'Enter') {
                var input = document.getElementById('console-input');
                var cmd = input.value.trim();
                if (!cmd) return;
                consoleCmdHistory.unshift(cmd);
                consoleCmdIndex = -1;
                input.value = '';
                consolePrint('> ' + cmd, 'var(--ardesia)');
                consoleRunCmd(cmd);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (consoleCmdIndex < consoleCmdHistory.length - 1) {
                    consoleCmdIndex++;
                    document.getElementById('console-input').value = consoleCmdHistory[consoleCmdIndex];
                }
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (consoleCmdIndex > 0) {
                    consoleCmdIndex--;
                    document.getElementById('console-input').value = consoleCmdHistory[consoleCmdIndex];
                } else {
                    consoleCmdIndex = -1;
                    document.getElementById('console-input').value = '';
                }
            }
        }
        
        function consolePrint(text, color) {
            var output = document.getElementById('console-output');
            var line = document.createElement('div');
            line.style.color = color || 'var(--inchiostro)';
            line.textContent = text;
            output.appendChild(line);
            output.scrollTop = output.scrollHeight;
        }
        
        function consoleRunCmd(cmd) {
            var parts = cmd.split(/\s+/);
            var command = parts[0].toLowerCase();
            
            if (command === 'help') {
                consolePrint('Commands:', 'var(--grafite)');
                consolePrint('  stats <field>                Stats for a field (or "tags" for file tags)', 'var(--grafite)');
                consolePrint('  stats audio                  Audio file statistics (duration, size, format)', 'var(--grafite)');
                consolePrint('  wordfreq <field> [limit]     Top words in a field (default: top 20)', 'var(--grafite)');
                consolePrint('  wordfreq <field> count       How many unique words in a field', 'var(--grafite)');
                consolePrint('  wordfreq <field> [limit] -s  With stop word filtering', 'var(--grafite)');
                consolePrint('  help                         Show this help', 'var(--grafite)');
                consolePrint('  clear                        Clear console output', 'var(--grafite)');
                return;
            }
            
            if (command === 'clear') {
                document.getElementById('console-output').innerHTML = '';
                return;
            }
            
            if (command === 'stats') {
                if (!parts[1]) {
                    consolePrint('Usage: stats <fieldname>', 'var(--grafite)');
                    consolePrint('       stats audio', 'var(--grafite)');
                    return;
                }
                var fieldName = parts[1];
                
                if (fieldName.toLowerCase() === 'audio') {
                    consolePrint('Scanning audio files...', 'var(--grafite)');
                    fetch('/api/stats/audio')
                        .then(function(resp) { return resp.json(); })
                        .then(function(data) {
                            consolePrint('Total files: ' + data.total_files);
                            if (data.files_not_found > 0) {
                                consolePrint('Files not found on disk: ' + data.files_not_found, 'var(--rosso)');
                            }
                            if (data.total_duration_readable) {
                                consolePrint('');
                                consolePrint('Duration:');
                                consolePrint('  Total:    ' + data.total_duration_readable);
                                consolePrint('  Average:  ' + data.avg_duration_sec + 's');
                                consolePrint('  Shortest: ' + data.min_duration_sec + 's (' + data.min_duration_file + ')');
                                consolePrint('  Longest:  ' + data.max_duration_sec + 's (' + data.max_duration_file + ')');
                            }
                            if (data.total_size_readable) {
                                consolePrint('');
                                consolePrint('Size:');
                                consolePrint('  Total:    ' + data.total_size_readable);
                                consolePrint('  Average:  ' + data.avg_size_readable);
                                consolePrint('  Smallest: ' + data.smallest_size_readable + ' (' + data.smallest_file + ')');
                                consolePrint('  Largest:  ' + data.largest_size_readable + ' (' + data.largest_file + ')');
                            }
                            if (data.by_format && data.by_format.length) {
                                consolePrint('');
                                consolePrint('Formats:');
                                for (var i = 0; i < data.by_format.length; i++) {
                                    consolePrint('  ' + data.by_format[i].format + ': ' + data.by_format[i].count);
                                }
                            }
                            if (data.by_sample_rate && data.by_sample_rate.length) {
                                consolePrint('');
                                consolePrint('Sample rates:');
                                for (var i = 0; i < data.by_sample_rate.length; i++) {
                                    consolePrint('  ' + data.by_sample_rate[i].rate + ' Hz: ' + data.by_sample_rate[i].count);
                                }
                            }
                            if (data.by_bit_depth && data.by_bit_depth.length) {
                                consolePrint('');
                                consolePrint('Bit depths:');
                                for (var i = 0; i < data.by_bit_depth.length; i++) {
                                    consolePrint('  ' + data.by_bit_depth[i].depth + '-bit: ' + data.by_bit_depth[i].count);
                                }
                            }
                        })
                        .catch(function(err) { consolePrint('Error: ' + err.message, 'var(--rosso)'); });
                    return;
                }
                
                fetch('/api/stats/' + encodeURIComponent(fieldName))
                    .then(function(resp) { return resp.json(); })
                    .then(function(data) {
                        if (data.error) {
                            consolePrint('Error: ' + data.error, 'var(--rosso)');
                            return;
                        }
                        consolePrint('Field: ' + data.field + ' (' + data.type + ')');
                        consolePrint('Files with value: ' + data.files_with_value + '/' + data.total_files);
                        
                        if (data.type === 'parameter') {
                            if (data.count > 0) {
                                consolePrint('Count:   ' + data.count);
                                consolePrint('Min:     ' + data.min);
                                consolePrint('Max:     ' + data.max);
                                consolePrint('Mean:    ' + data.mean);
                                consolePrint('Median:  ' + data.median);
                                consolePrint('Std Dev: ' + data.std_dev);
                                consolePrint('Sum:     ' + data.sum);
                            } else {
                                consolePrint('No numeric values found.', 'var(--grafite)');
                            }
                        } else if (data.type === 'choice') {
                            if (data.options) {
                                for (var i = 0; i < data.options.length; i++) {
                                    var o = data.options[i];
                                    consolePrint('  ' + o.value + ': ' + o.count + ' (' + o.percent + '%)');
                                }
                            }
                        } else if (data.type === 'tags' || data.type === 'file_tags') {
                            consolePrint('Unique tags: ' + data.unique_tags);
                            if (data.tags) {
                                for (var i = 0; i < data.tags.length; i++) {
                                    var t = data.tags[i];
                                    var pct = t.percent ? ' (' + t.percent + '%)' : '';
                                    consolePrint('  ' + t.tag + ': ' + t.count + pct);
                                }
                            }
                        } else if (data.type === 'markers') {
                            consolePrint('Total markers: ' + data.total_markers);
                            consolePrint('Files with markers: ' + data.files_with_markers);
                            consolePrint('Avg per file: ' + data.avg_per_file);
                            if (data.by_color && data.by_color.length) {
                                consolePrint('By color:');
                                for (var i = 0; i < data.by_color.length; i++) {
                                    consolePrint('  ' + data.by_color[i].color + ': ' + data.by_color[i].count);
                                }
                            }
                        }
                    })
                    .catch(function(err) { consolePrint('Error: ' + err.message, 'var(--rosso)'); });
                return;
            }
            
            if (command === 'wordfreq') {
                if (!parts[1]) {
                    consolePrint('Usage: wordfreq <fieldname> [limit] [-s]', 'var(--grafite)');
                    consolePrint('       wordfreq <fieldname> count', 'var(--grafite)');
                    return;
                }
                var fieldName = parts[1];
                var useStopwords = cmd.indexOf(' -s') !== -1 ? '1' : '0';
                
                if (parts[2] && parts[2].toLowerCase() === 'count') {
                    fetch('/api/stats/' + encodeURIComponent(fieldName) + '/wordfreq?countonly=1')
                        .then(function(resp) { return resp.json(); })
                        .then(function(data) {
                            if (data.error) { consolePrint('Error: ' + data.error, 'var(--rosso)'); return; }
                            consolePrint('Field: ' + data.field + ' (' + data.type + ')');
                            consolePrint('Unique words: ' + data.unique_words);
                        })
                        .catch(function(err) { consolePrint('Error: ' + err.message, 'var(--rosso)'); });
                    return;
                }
                
                var limit = 20;
                if (parts[2] && !isNaN(parseInt(parts[2]))) {
                    limit = parseInt(parts[2]);
                }
                
                fetch('/api/stats/' + encodeURIComponent(fieldName) + '/wordfreq?limit=' + limit + '&stopwords=' + useStopwords)
                    .then(function(resp) { return resp.json(); })
                    .then(function(data) {
                        if (data.error) { consolePrint('Error: ' + data.error, 'var(--rosso)'); return; }
                        consolePrint('Field: ' + data.field + ' (' + data.type + ')');
                        consolePrint('Unique words: ' + data.unique_words);
                        consolePrint('Showing top ' + data.showing + ':');
                        consolePrint('');
                        for (var i = 0; i < data.words.length; i++) {
                            var w = data.words[i];
                            var rank = String(i + 1);
                            while (rank.length < String(data.showing).length) rank = ' ' + rank;
                            consolePrint('  ' + rank + '. ' + w.word + '  (' + w.count + ')');
                        }
                    })
                    .catch(function(err) { consolePrint('Error: ' + err.message, 'var(--rosso)'); });
                return;
            }
            
            consolePrint('Unknown command: ' + command, 'var(--rosso)');
            consolePrint('Type "help" for available commands.', 'var(--grafite)');
        }
        
        function showFileStats() {
            if (!selectedFileId) return;
            
            fetch('/api/file/' + selectedFileId + '/stats')
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.error) { alert(data.error); return; }
                    
                    var panel = document.getElementById('metadata-panel');
                    var html = '<div style="padding: 4px;">';
                    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">';
                    html += '<span style="font-weight:600;color:var(--inchiostro);">File Stats: ' + data.filename + '</span>';
                    html += '<button class="btn btn-secondary" onclick="loadFileMetadata(selectedFileId)" style="font-size:11px;padding:2px 8px;">Back</button>';
                    html += '</div>';
                    
                    // Audio info
                    if (data.duration_sec !== undefined) {
                        var mins = Math.floor(data.duration_sec / 60);
                        var secs = Math.round(data.duration_sec % 60);
                        html += '<div class="form-group">';
                        html += '<label>Audio</label>';
                        html += '<div style="color:var(--grafite);font-size:13px;line-height:1.8;">';
                        html += 'Duration: ' + mins + ':' + (secs < 10 ? '0' : '') + secs + '<br>';
                        html += 'Sample rate: ' + data.sample_rate + ' Hz<br>';
                        html += 'Bit depth: ' + data.bit_depth + '-bit<br>';
                        html += 'Channels: ' + data.channels + '<br>';
                        html += 'Size: ' + data.file_size_readable;
                        html += '</div></div>';
                    }
                    
                    // Tags
                    if (data.tags && data.tags.length > 0) {
                        html += '<div class="form-group">';
                        html += '<label>Tags (' + data.tag_count + ')</label>';
                        html += '<div style="color:var(--grafite);font-size:13px;">';
                        for (var i = 0; i < data.tags.length; i++) {
                            html += '<span class="tag">' + data.tags[i] + '</span> ';
                        }
                        html += '</div></div>';
                    }
                    
                    // Per-field stats
                    var fieldNames = Object.keys(data.fields);
                    for (var f = 0; f < fieldNames.length; f++) {
                        var fname = fieldNames[f];
                        var fdata = data.fields[fname];
                        
                        html += '<div class="form-group">';
                        html += '<label>' + fname + ' (' + fdata.type + ')</label>';
                        html += '<div style="color:var(--grafite);font-size:13px;line-height:1.8;">';
                        
                        if (fdata.type === 'markers') {
                            html += 'Markers: ' + fdata.marker_count + '<br>';
                            html += 'Unique words: ' + fdata.unique_words + '<br>';
                            if (fdata.by_color && fdata.by_color.length) {
                                html += 'Colors: ';
                                for (var c = 0; c < fdata.by_color.length; c++) {
                                    if (c > 0) html += ', ';
                                    html += fdata.by_color[c].color + ' (' + fdata.by_color[c].count + ')';
                                }
                                html += '<br>';
                            }
                            if (fdata.top_words && fdata.top_words.length) {
                                html += '<br>Top words:<br>';
                                for (var w = 0; w < fdata.top_words.length; w++) {
                                    html += '  ' + fdata.top_words[w].word + ': ' + fdata.top_words[w].count + '<br>';
                                }
                            }
                        } else if (fdata.type === 'datalink' || fdata.type === 'text') {
                            html += 'Total words: ' + fdata.total_words + '<br>';
                            html += 'Unique words: ' + fdata.unique_words + '<br>';
                            if (fdata.top_words && fdata.top_words.length) {
                                html += '<br>Top words:<br>';
                                for (var w = 0; w < fdata.top_words.length; w++) {
                                    html += '  ' + fdata.top_words[w].word + ': ' + fdata.top_words[w].count + '<br>';
                                }
                            }
                        } else if (fdata.type === 'tags') {
                            html += 'Tags: ' + fdata.tag_count + '<br>';
                            for (var t = 0; t < fdata.tags.length; t++) {
                                html += '<span class="tag">' + fdata.tags[t] + '</span> ';
                            }
                        } else if (fdata.type === 'parameter') {
                            html += 'Value: ' + fdata.value;
                        } else if (fdata.type === 'choice') {
                            html += 'Value: ' + fdata.value;
                        }
                        
                        html += '</div></div>';
                    }
                    
                    html += '</div>';
                    panel.innerHTML = html;
                })
                .catch(function(err) { alert('Error: ' + err.message); });
        }
        
        function toggleQuery() {
            const panel = document.getElementById('query-panel');
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        }
        
        function addQueryCondition() {
            queryConditionCount++;
            const container = document.getElementById('query-conditions');
            const div = document.createElement('div');
            div.className = 'query-condition';
            div.style.cssText = 'display: flex; gap: 8px; align-items: center; margin-bottom: 8px;';
            div.innerHTML = `
                <select style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                    <option value="AND">AND</option>
                    <option value="OR">OR</option>
                </select>
                <select id="qfield-${queryConditionCount}" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                    <option value="">Select field...</option>
                </select>
                <select id="qop-${queryConditionCount}" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                    <option value="=">=</option>
                    <option value=">">></option>
                    <option value="<"><</option>
                    <option value="contains">contains</option>
                </select>
                <input type="text" id="qval-${queryConditionCount}" placeholder="value" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft); width: 150px;">
                <button onclick="this.parentElement.remove()" style="background: var(--rosso); color: white; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer;">×</button>
            `;
            container.appendChild(div);
            loadFields(); // Refresh dropdowns
        }
        
        function runQuery() {
            // Collect all query conditions
            const conditions = [];
            document.querySelectorAll('.query-condition').forEach((div, idx) => {
                const fieldSel = div.querySelector('[id^="qfield-"]');
                const opSel = div.querySelector('[id^="qop-"]');
                const valInput = div.querySelector('[id^="qval-"]');
                const logicSel = div.querySelector('select[style*="background"]');
                
                if (fieldSel && fieldSel.value && valInput && valInput.value) {
                    conditions.push({
                        field_id: fieldSel.value,
                        operator: opSel.value,
                        value: valInput.value,
                        logic: idx > 0 && logicSel ? logicSel.value : 'AND'
                    });
                }
            });
            
            const sortField = document.getElementById('sort-field').value;
            const sortOrder = document.getElementById('sort-order').value;
            
            // Send query to backend
            fetch('/api/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    conditions: conditions,
                    sort_by: sortField,
                    sort_order: sortOrder
                })
            })
            .then(r => r.json())
            .then(result => {
                document.getElementById('query-results').textContent = 
                    `Found ${result.files.length} file(s) matching your query`;
                
                // Update allFiles with results for rendering
                allFiles = result.files;
                renderFiles();
            });
        }
        
        function clearQuery() {
            document.getElementById('query-conditions').innerHTML = `
                <div class="query-condition" style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
                    <select id="qfield-0" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                        <option value="">Select field...</option>
                    </select>
                    <select id="qop-0" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft);">
                        <option value="=">=</option>
                        <option value=">">></option>
                        <option value="<"><</option>
                        <option value="contains">contains</option>
                    </select>
                    <input type="text" id="qval-0" placeholder="value" style="background: var(--calce); color: var(--inchiostro); padding: 6px; border-radius: 4px; border: 1px solid var(--rule-soft); width: 150px;">
                </div>
            `;
            document.getElementById('query-results').textContent = '';
            document.getElementById('sort-field').value = '';
            document.getElementById('sort-order').value = 'asc';
            queryConditionCount = 0;
            loadFields();
            loadFiles(); // Reset to show all files
        }
        
        function saveProject() { saveExport('json', 'Project export'); }

        function exportForMax() { saveExport('max', 'Max export'); }

        // ---- Quadro dropdowns ----
        // Native <select> menus open as the system pop-up, which can't be styled.
        // Each select gets a Quadro button and menu; the real select stays (hidden)
        // underneath, so values, change events and existing code keep working.
        (function () {
            var openState = null;              // { sel, menu, items, active }
            function labelOf(sel) {
                var o = sel.options[sel.selectedIndex];
                return o ? o.text : '';
            }
            function sync(sel) {
                var q = sel._qsel;
                if (!q) return;
                q.label.textContent = labelOf(sel) || ' ';
                q.btn.disabled = sel.disabled;
            }
            function enhance(sel) {
                if (sel._qsel || sel.multiple || sel.size > 1) return;
                var wrap = document.createElement('span');
                wrap.className = 'qsel' + (sel.closest('.speed-glass') ? ' qsel-glass' : '');
                var st = sel.style;
                ['width', 'flex', 'minWidth', 'maxWidth', 'marginLeft', 'marginRight'].forEach(function (k) {
                    if (st[k]) wrap.style[k] = st[k];
                });
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'qsel-btn';
                if (sel.title) btn.title = sel.title;
                var label = document.createElement('span');
                label.className = 'qsel-label';
                var caret = document.createElement('span');
                caret.className = 'qsel-caret';
                caret.textContent = '▾';
                btn.appendChild(label);
                btn.appendChild(caret);
                sel.parentNode.insertBefore(wrap, sel);
                wrap.appendChild(sel);
                wrap.appendChild(btn);
                sel.classList.add('qsel-native');
                sel.tabIndex = -1;
                sel._qsel = { wrap: wrap, btn: btn, label: label };
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (openState && openState.sel === sel) close();
                    else open(sel);
                });
                btn.addEventListener('keydown', function (e) {
                    if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        e.stopPropagation();
                        open(sel);
                    }
                });
                sel.addEventListener('change', function () { sync(sel); });
                new MutationObserver(function () { sync(sel); }).observe(sel, { childList: true, subtree: true, attributes: true, characterData: true });
                sync(sel);
            }
            function open(sel) {
                close();
                if (sel.disabled) return;
                var q = sel._qsel;
                var menu = document.createElement('div');
                menu.className = 'qsel-menu' + (q.wrap.classList.contains('qsel-glass') ? ' qsel-menu-glass' : '');
                var items = [];
                for (var i = 0; i < sel.options.length; i++) {
                    var o = sel.options[i];
                    if (o.hidden) continue;
                    var it = document.createElement('div');
                    it.className = 'qsel-item' + (i === sel.selectedIndex ? ' is-selected' : '') + (o.disabled ? ' is-disabled' : '');
                    it.innerHTML = '<span class="tool-lamp"></span><span class="qsel-item-text"></span>';
                    it.lastChild.textContent = o.text || ' ';
                    it.setAttribute('data-index', i);
                    (function (idx, disabled) {
                        it.addEventListener('mousedown', function (e) { e.preventDefault(); });
                        it.addEventListener('click', function (e) { e.stopPropagation(); if (!disabled) choose(sel, idx); });
                        it.addEventListener('mousemove', function () { setActive(items.indexOf(this)); });
                    })(i, o.disabled);
                    menu.appendChild(it);
                    items.push(it);
                }
                document.body.appendChild(menu);
                var r = q.btn.getBoundingClientRect();
                menu.style.minWidth = Math.max(r.width, 120) + 'px';
                var mh = menu.offsetHeight;
                var below = window.innerHeight - r.bottom - 8;
                var top = (mh <= below || r.top < mh) ? r.bottom + 2 : r.top - mh - 2;
                menu.style.top = Math.max(4, top) + 'px';
                menu.style.left = Math.max(4, Math.min(r.left, window.innerWidth - menu.offsetWidth - 4)) + 'px';
                menu.style.maxHeight = Math.max(120, (mh <= below || r.top < mh) ? below : r.top - 8) + 'px';
                q.wrap.classList.add('is-open');
                var startIdx = 0;
                for (var k = 0; k < items.length; k++) if (+items[k].getAttribute('data-index') === sel.selectedIndex) startIdx = k;
                openState = { sel: sel, menu: menu, items: items, active: -1, typed: '', typedAt: 0 };
                setActive(startIdx);
            }
            function setActive(k) {
                if (!openState || k < 0 || k >= openState.items.length) return;
                if (openState.active >= 0) openState.items[openState.active].classList.remove('is-active');
                openState.active = k;
                var it = openState.items[k];
                it.classList.add('is-active');
                var m = openState.menu;
                if (it.offsetTop < m.scrollTop) m.scrollTop = it.offsetTop;
                else if (it.offsetTop + it.offsetHeight > m.scrollTop + m.clientHeight) m.scrollTop = it.offsetTop + it.offsetHeight - m.clientHeight;
            }
            function choose(sel, idx) {
                close();
                if (sel.selectedIndex !== idx) {
                    sel.selectedIndex = idx;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }
                sync(sel);
                if (sel._qsel && sel._qsel.btn.isConnected) sel._qsel.btn.focus();
            }
            function close() {
                if (!openState) return;
                if (openState.menu.parentNode) openState.menu.parentNode.removeChild(openState.menu);
                if (openState.sel._qsel) openState.sel._qsel.wrap.classList.remove('is-open');
                openState = null;
            }
            // While a menu is open, the keyboard drives the menu (and nothing else).
            window.addEventListener('keydown', function (e) {
                if (!openState) return;
                var st = openState, n = st.items.length;
                var handled = true;
                if (e.key === 'ArrowDown') setActive(Math.min(n - 1, st.active + 1));
                else if (e.key === 'ArrowUp') setActive(Math.max(0, st.active - 1));
                else if (e.key === 'Home') setActive(0);
                else if (e.key === 'End') setActive(n - 1);
                else if (e.key === 'Enter' || e.key === ' ') {
                    var it = st.items[st.active];
                    if (it && !it.classList.contains('is-disabled')) choose(st.sel, +it.getAttribute('data-index'));
                }
                else if (e.key === 'Escape' || e.key === 'Tab') { var b = st.sel._qsel.btn; close(); b.focus(); }
                else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
                    var now = Date.now();
                    st.typed = (now - st.typedAt < 700 ? st.typed : '') + e.key.toLowerCase();
                    st.typedAt = now;
                    for (var k = 0; k < n; k++) {
                        if (st.items[k].textContent.trim().toLowerCase().indexOf(st.typed) === 0) { setActive(k); break; }
                    }
                }
                else handled = false;
                if (handled) { e.preventDefault(); e.stopPropagation(); }
            }, true);
            document.addEventListener('mousedown', function (e) {
                if (openState && !openState.menu.contains(e.target) && !openState.sel._qsel.btn.contains(e.target)) close();
            }, true);
            window.addEventListener('resize', close);
            document.addEventListener('scroll', function (e) {
                if (openState && !openState.menu.contains(e.target)) close();
            }, true);

            // Code that sets select.value / selectedIndex directly updates the button too.
            ['value', 'selectedIndex'].forEach(function (prop) {
                var d = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, prop);
                if (!d || !d.set) return;
                Object.defineProperty(HTMLSelectElement.prototype, prop, {
                    configurable: true,
                    enumerable: d.enumerable,
                    get: d.get,
                    set: function (v) { d.set.call(this, v); if (this._qsel) sync(this); }
                });
            });

            function scan(root) {
                if (root.tagName === 'SELECT') { enhance(root); return; }
                if (root.querySelectorAll) {
                    var list = root.querySelectorAll('select');
                    for (var i = 0; i < list.length; i++) enhance(list[i]);
                }
            }
            scan(document.body);
            new MutationObserver(function (muts) {
                muts.forEach(function (m) {
                    for (var i = 0; i < m.addedNodes.length; i++) {
                        if (m.addedNodes[i].nodeType === 1) scan(m.addedNodes[i]);
                    }
                    if (openState && !openState.sel.isConnected) close();
                });
            }).observe(document.body, { childList: true, subtree: true });
        })();
    </script>
</body>
</html>
'''

# API Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/files')
def get_files():
    return jsonify(db.get_files())

@app.route('/api/file/<int:file_id>')
def get_file(file_id):
    return jsonify(db.get_file(file_id))

@app.route('/api/file/<int:file_id>', methods=['DELETE'])
def delete_file_route(file_id):
    db.delete_file(file_id)
    return jsonify({'success': True})

@app.route('/api/fields')
def get_fields():
    return jsonify(db.get_fields())

@app.route('/api/field', methods=['POST'])
def add_field():
    data = request.json
    field_id = db.add_field(
        data['name'],
        data['type'],
        data.get('min_val'),
        data.get('max_val'),
        data.get('choices')
    )
    return jsonify({'id': field_id})

@app.route('/api/field/<int:field_id>', methods=['DELETE'])
def delete_field_route(field_id):
    db.delete_field(field_id)
    return jsonify({'success': True})

# ----- Group routes ---------------------------------------------------------
@app.route('/api/groups', methods=['GET'])
def list_groups_route():
    return jsonify(db.get_groups())

@app.route('/api/groups', methods=['POST'])
def create_group_route():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required'}), 400
    parent_id = data.get('parent_id')
    gid = db.add_group(name, parent_id)
    return jsonify({'success': True, 'id': gid})

@app.route('/api/groups/<int:group_id>', methods=['PUT'])
def update_group_route(group_id):
    data = request.json or {}
    if 'name' in data:
        new_name = (data['name'] or '').strip()
        if not new_name:
            return jsonify({'success': False, 'error': 'Name cannot be empty'}), 400
        if not db.rename_group(group_id, new_name):
            return jsonify({'success': False, 'error': 'Group not found'}), 404
    if 'parent_id' in data:
        if not db.reparent_group(group_id, data['parent_id']):
            return jsonify({'success': False, 'error': 'Reparent would create a cycle or group missing'}), 400
    return jsonify({'success': True})

@app.route('/api/groups/<int:group_id>', methods=['DELETE'])
def delete_group_route(group_id):
    if not db.delete_group(group_id):
        return jsonify({'success': False, 'error': 'Group not found'}), 404
    return jsonify({'success': True})

@app.route('/api/files/group', methods=['POST'])
def set_files_group_route():
    """Assign files to a group. group_id may be null to ungroup."""
    data = request.json or {}
    file_ids = data.get('file_ids') or []
    group_id = data.get('group_id')  # may be None
    if not file_ids:
        return jsonify({'success': False, 'error': 'No file_ids provided'}), 400
    db.set_file_group_batch(file_ids, group_id)
    return jsonify({'success': True})

@app.route('/api/field/<int:field_id>', methods=['PUT'])
def rename_field_route(field_id):
    new_name = (request.json or {}).get('name', '').strip()
    if not new_name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    ok = db.rename_field(field_id, new_name)
    if not ok:
        return jsonify({'success': False, 'error': 'Name already in use or field not found'}), 400
    return jsonify({'success': True})

@app.route('/api/file/<int:file_id>/tag', methods=['POST'])
def add_tag_route(file_id):
    tag = request.json['tag']
    db.add_tag(file_id, tag)
    return jsonify({'success': True})

@app.route('/api/file/<int:file_id>/tag', methods=['DELETE'])
def remove_tag_route(file_id):
    tag = request.json['tag']
    db.remove_tag(file_id, tag)
    return jsonify({'success': True})

@app.route('/api/file/<int:file_id>/value', methods=['POST'])
def set_value_route(file_id):
    data = request.json
    field_id = data['field_id']
    value = data['value']
    
    # Check if this is a datalink field - if so, read and index the file content
    fields = db.get_fields()
    field = next((f for f in fields if f['id'] == field_id), None)
    
    content = None
    if field and field['type'] == 'datalink' and value:
        content = read_file_content(value)
        if content:
            print(f"Indexed {len(content)} chars from: {value}")
    
    db.set_value(file_id, field_id, value, content)
    return jsonify({'success': True, 'indexed': content is not None})

@app.route('/api/files/batch/tag', methods=['POST'])
def batch_add_tag():
    data = request.json
    db.add_tags_batch(data['file_ids'], data['tag'])
    return jsonify({'success': True})

@app.route('/api/files/batch/value', methods=['POST'])
def batch_set_value():
    data = request.json
    db.set_value_batch(data['file_ids'], data['field_id'], data['value'])
    return jsonify({'success': True})

@app.route('/api/files/batch/delete', methods=['POST'])
def batch_delete_files():
    data = request.json
    db.delete_files(data['file_ids'])
    return jsonify({'success': True})

# =====================================================================
# File-embedded metadata extraction
# =====================================================================
# Reads metadata from the audio file itself: filesystem mtime, WAV BWF
# `bext` chunk, WAV `iXML` chunk, basic PCM WAV header fields, and (if the
# `mutagen` library is installed) ID3 / Vorbis / MP4 / FLAC tags. Returns a
# uniform dict of {field_name: {"type": "text"|"date", "value": "..."}}.

def _read_wav_chunks(filepath):
    """Read selected RIFF chunks from a WAV file. Returns dict of bytes."""
    chunks = {}
    try:
        with open(filepath, 'rb') as f:
            if f.read(4) != b'RIFF':
                return chunks
            f.read(4)  # file size
            if f.read(4) != b'WAVE':
                return chunks
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                chunk_id = hdr[:4]
                chunk_size = int.from_bytes(hdr[4:], 'little')
                if chunk_id in (b'bext', b'iXML'):
                    chunks[chunk_id.decode('ascii', errors='replace')] = f.read(chunk_size)
                    if chunk_size % 2:
                        f.read(1)
                else:
                    # skip; RIFF chunks are padded to even sizes
                    f.seek(chunk_size + (chunk_size % 2), 1)
    except Exception:
        pass
    return chunks


def _parse_bext(data):
    """Parse a BWF bext chunk (EBU Tech 3285) into a small dict of strings."""
    out = {}
    if not data or len(data) < 338:
        return out
    def _str(buf):
        return buf.split(b'\x00', 1)[0].decode('latin-1', errors='replace').strip()
    try:
        out['Description'] = _str(data[0:256])
        out['Originator'] = _str(data[256:288])
        out['Originator Reference'] = _str(data[288:320])
        out['Origination Date'] = data[320:330].decode('ascii', errors='replace').strip()
        out['Origination Time'] = data[330:338].decode('ascii', errors='replace').strip()
    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


def _parse_ixml(data):
    """Pull a few common scene/take fields out of an iXML WAV chunk."""
    out = {}
    if not data:
        return out
    try:
        text = data.decode('utf-8', errors='replace')
        start = text.find('<')
        end = text.rfind('>')
        if start == -1 or end == -1 or end < start:
            return out
        text = text[start:end + 1]
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        wanted = ['PROJECT', 'SCENE', 'TAKE', 'TAPE', 'NOTE', 'CIRCLED',
                  'WILD_TRACK', 'NO_GOOD', 'FALSE_START']
        for tag in wanted:
            elem = root.find(tag)
            if elem is not None and elem.text and elem.text.strip():
                out['iXML ' + tag.replace('_', ' ').title()] = elem.text.strip()
    except Exception:
        pass
    return out


def extract_file_metadata(filepath):
    """Return {field_name: {'type': 'text'|'date', 'value': str}} for one file."""
    result = {}
    if not filepath or not os.path.exists(filepath):
        return result

    # Filesystem mtime is universally available.
    try:
        st = os.stat(filepath)
        result['File Modified'] = {
            'type': 'date',
            'value': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d'),
        }
        result['File Size (MB)'] = {
            'type': 'text',
            'value': f"{st.st_size / 1024 / 1024:.2f}",
        }
    except OSError:
        pass

    ext = os.path.splitext(filepath)[1].lower()

    # WAV: standard PCM header via stdlib `wave`, plus BWF/iXML chunks.
    if ext in ('.wav', '.wave'):
        try:
            import wave
            with wave.open(filepath, 'rb') as w:
                rate = w.getframerate()
                frames = w.getnframes()
                result['Sample Rate'] = {'type': 'text', 'value': str(rate)}
                result['Channels'] = {'type': 'text', 'value': str(w.getnchannels())}
                result['Bit Depth'] = {'type': 'text', 'value': str(w.getsampwidth() * 8)}
                if rate:
                    result['Duration (s)'] = {'type': 'text', 'value': f"{frames / rate:.3f}"}
        except Exception:
            pass

        chunks = _read_wav_chunks(filepath)
        for k, v in _parse_bext(chunks.get('bext', b'')).items():
            ftype = 'date' if k == 'Origination Date' else 'text'
            result['BWF ' + k] = {'type': ftype, 'value': v}
        for k, v in _parse_ixml(chunks.get('iXML', b'')).items():
            result[k] = {'type': 'text', 'value': v}

    # Mutagen handles ID3 (mp3), Vorbis (flac/ogg), MP4 (m4a), AIFF, etc.
    try:
        import mutagen
        m = mutagen.File(filepath, easy=True)
        if m is not None:
            tag_map = {
                'title': ('Title', 'text'),
                'artist': ('Artist', 'text'),
                'album': ('Album', 'text'),
                'date': ('Date', 'date'),
                'genre': ('Genre', 'text'),
                'comment': ('Comment', 'text'),
                'tracknumber': ('Track Number', 'text'),
                'composer': ('Composer', 'text'),
                'albumartist': ('Album Artist', 'text'),
            }
            for key, (field_name, ftype) in tag_map.items():
                vals = m.get(key)
                if not vals:
                    continue
                val = vals[0] if isinstance(vals, list) else vals
                val = str(val).strip()
                if not val:
                    continue
                # ID3 dates are sometimes just years; coerce to YYYY-MM-DD if possible.
                if ftype == 'date':
                    if len(val) == 4 and val.isdigit():
                        val = f"{val}-01-01"
                    elif len(val) == 7 and val[:4].isdigit() and val[4] == '-':
                        val = val + '-01'
                result[field_name] = {'type': ftype, 'value': val}
            # Audio properties (sample rate / channels) for non-WAV formats
            info = getattr(m, 'info', None)
            if info is not None:
                if 'Sample Rate' not in result and getattr(info, 'sample_rate', None):
                    result['Sample Rate'] = {'type': 'text', 'value': str(info.sample_rate)}
                if 'Channels' not in result and getattr(info, 'channels', None):
                    result['Channels'] = {'type': 'text', 'value': str(info.channels)}
                if 'Duration (s)' not in result and getattr(info, 'length', None):
                    result['Duration (s)'] = {'type': 'text', 'value': f"{info.length:.3f}"}
    except ImportError:
        pass
    except Exception:
        pass

    return result


@app.route('/api/files/import-metadata', methods=['POST'])
def import_file_metadata_route():
    """Read embedded/file-system metadata for the given file_ids and write it
    into matching fields. Creates fields if they don't already exist. By default
    (overwrite=False) values are only written where no value exists yet, so
    manual edits aren't clobbered."""
    body = request.json or {}
    file_ids = body.get('file_ids') or []
    overwrite = bool(body.get('overwrite', False))
    if not file_ids:
        return jsonify({'success': False, 'error': 'No file_ids provided'}), 400

    # Step 1: extract metadata for each file (skip files that no longer exist).
    extracted = {}  # file_id -> {field_name: {type, value}}
    file_records = {}  # file_id -> file dict (used for existing-value check)
    fields_seen = {}  # field_name -> field_type (for field creation)
    skipped_missing = 0
    for fid in file_ids:
        rec = db.get_file(fid)
        if not rec:
            continue
        if not rec.get('filepath') or not os.path.exists(rec['filepath']):
            skipped_missing += 1
            continue
        meta = extract_file_metadata(rec['filepath'])
        if meta:
            extracted[fid] = meta
            file_records[fid] = rec
            for name, info in meta.items():
                # Don't downgrade a date-typed field to text if seen later
                if name not in fields_seen or fields_seen[name] == 'text':
                    fields_seen[name] = info['type']

    # Step 2: ensure each needed field exists.
    existing = {f['name']: (f['id'], f['type']) for f in db.get_fields()}
    field_ids = {}
    fields_created = 0
    for name, ftype in fields_seen.items():
        if name in existing:
            field_ids[name] = existing[name][0]
        else:
            new_id = db.add_field(name, ftype)
            if new_id:
                field_ids[name] = new_id
                fields_created += 1

    # Step 3: write values (per-file, since values vary).
    values_set = 0
    values_skipped = 0
    for fid, meta in extracted.items():
        existing_values = file_records[fid].get('fields', {}) or {}
        for name, info in meta.items():
            field_id = field_ids.get(name)
            if not field_id or not info.get('value'):
                continue
            if not overwrite and existing_values.get(name):
                values_skipped += 1
                continue
            db.set_value(fid, field_id, info['value'])
            values_set += 1

    return jsonify({
        'success': True,
        'files_processed': len(extracted),
        'files_skipped_missing': skipped_missing,
        'fields_created': fields_created,
        'values_set': values_set,
        'values_skipped_existing': values_skipped,
    })


# ---------------------------------------------------------------------------
# Native file dialogs (Finder / Explorer), opened by the app window.
# Not available when Earmark runs in a normal browser: the page then keeps
# its typed-path boxes.
# ---------------------------------------------------------------------------
_ui_window = None
AUDIO_EXTENSIONS = ['.wav', '.mp3', '.m4a', '.flac', '.aiff', '.aif', '.ogg', '.wave', '.bwf']


@app.route('/api/dialog', methods=['GET', 'POST'])
def native_dialog():
    """GET: are native dialogs available?  POST {kind, filename?}: open one.

    kind: 'folder' (choose a folder), 'files' (one or more audio files),
          'db' (open a .db), 'savedb' (save-as a .db, filename = suggestion).
    Returns {available, paths: [...]}; paths is empty if cancelled.
    """
    if _ui_window is None:
        return jsonify({'available': False, 'paths': []})
    if request.method == 'GET':
        return jsonify({'available': True})
    import webview
    data = request.json or {}
    kind = data.get('kind', 'folder')
    FD = getattr(webview, 'FileDialog', None)
    FOLDER = FD.FOLDER if FD else webview.FOLDER_DIALOG
    OPEN = FD.OPEN if FD else webview.OPEN_DIALOG
    SAVE = FD.SAVE if FD else webview.SAVE_DIALOG

    dirs = _load_settings().get('dialog_dirs', {})
    if kind in ('db', 'savedb'):
        start = dirs.get(kind) or os.path.dirname(os.path.abspath(current_db_path)) or DATA_DIR
    else:
        start = dirs.get(kind) or os.path.expanduser('~')
    if not os.path.isdir(start):
        start = os.path.expanduser('~')

    try:
        if kind == 'folder':
            res = _ui_window.create_file_dialog(FOLDER, directory=start)
        elif kind == 'files':
            pattern = ';'.join('*' + e for e in AUDIO_EXTENSIONS)
            res = _ui_window.create_file_dialog(OPEN, directory=start, allow_multiple=True,
                                                file_types=('Audio files (%s)' % pattern, 'All files (*.*)'))
        elif kind == 'db':
            res = _ui_window.create_file_dialog(OPEN, directory=start,
                                                file_types=('Earmark database (*.db)', 'All files (*.*)'))
        elif kind == 'savedb':
            name = os.path.basename(data.get('filename') or 'archive.db')
            res = _ui_window.create_file_dialog(SAVE, directory=start, save_filename=name)
        else:
            return jsonify({'available': True, 'paths': [], 'error': 'Unknown dialog: ' + str(kind)}), 400
    except Exception as e:
        return jsonify({'available': True, 'paths': [], 'error': str(e)}), 500

    if not res:
        paths = []
    elif isinstance(res, str):
        paths = [res]
    else:
        paths = [str(x) for x in res]
    if paths:
        first = paths[0]
        remember = first if kind == 'folder' else os.path.dirname(first)
        dirs[kind] = remember
        _save_settings({'dialog_dirs': dirs})
    return jsonify({'available': True, 'paths': paths})


@app.route('/api/import', methods=['POST'])
def import_files():
    """Import audio: a folder ({path, recursive}) or a list of files ({paths})."""
    data = request.json or {}
    imported = 0
    skipped = 0

    files = data.get('paths')
    if files:
        for f in files:
            fp = Path(f)
            if fp.is_file() and fp.suffix.lower() in AUDIO_EXTENSIONS:
                if db.add_file(str(fp)):
                    imported += 1
                else:
                    skipped += 1       # already in the database
        return jsonify({'imported': imported, 'skipped': skipped})

    folder_path = data.get('path', '')
    recursive = data.get('recursive', False)
    # Use ** for recursive, * for single folder
    pattern = '**/*' if recursive else '*'
    for file in Path(folder_path).glob(pattern):
        if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS:
            if db.add_file(str(file)):
                imported += 1
            else:
                skipped += 1
    return jsonify({'imported': imported, 'skipped': skipped})

@app.route('/api/link-metadata', methods=['POST'])
def link_metadata_files():
    """Link metadata files to audio files by matching prefixes.
    
    Example: rec_001.wav will match rec_001_transcription.txt, rec_001_pitchtrack.csv, etc.
    """
    folder_path = request.json['path']
    field_name = request.json['fieldName']
    recursive = request.json.get('recursive', False)
    text_extensions = ['.txt', '.TextGrid', '.csv', '.md', '.json', '.eaf', '.xml']
    
    # Get or create the field
    fields = db.get_fields()
    field = next((f for f in fields if f['name'] == field_name), None)
    
    if not field:
        field_id = db.add_field(field_name, 'datalink')
        if field_id:
            field = {'id': field_id, 'name': field_name, 'type': 'datalink'}
        else:
            return jsonify({'error': 'Could not create field'}), 400
    
    # Get all audio files in database
    all_files = db.get_files()
    
    # Build a lookup by filename prefix (without extension)
    audio_lookup = {}
    for f in all_files:
        # Get the base name without extension
        base = Path(f['filename']).stem
        audio_lookup[base] = f
    
    linked = 0
    not_found = 0
    
    # Use ** for recursive, * for single folder
    pattern = '**/*' if recursive else '*'
    
    for meta_file in Path(folder_path).glob(pattern):
        if meta_file.is_file() and meta_file.suffix.lower() in text_extensions:
            # Try to match by prefix
            meta_stem = meta_file.stem
            
            # Find matching audio file
            matched_audio = None
            
            # First try exact match
            if meta_stem in audio_lookup:
                matched_audio = audio_lookup[meta_stem]
            else:
                # Try prefix match (meta_stem starts with audio_stem)
                for audio_stem, audio_file in audio_lookup.items():
                    if meta_stem.startswith(audio_stem):
                        # Check it's followed by underscore, dash, or end
                        rest = meta_stem[len(audio_stem):]
                        if rest == '' or rest[0] in ['_', '-', '.']:
                            matched_audio = audio_file
                            break
            
            if matched_audio:
                # Read and index the content
                content = read_file_content(str(meta_file))
                db.set_value(matched_audio['id'], field['id'], str(meta_file), content)
                linked += 1
            else:
                not_found += 1
    
    return jsonify({'linked': linked, 'notFound': not_found})

@app.route('/api/audio/<int:file_id>')
def serve_audio(file_id):
    """Serve audio file for playback"""
    file_data = db.get_file(file_id)
    if not file_data:
        return jsonify({'error': 'File not found'}), 404
    
    filepath = file_data['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Audio file not found on disk'}), 404
    
    # Use send_file which handles range requests properly for audio
    return send_file(filepath, mimetype='audio/wav', conditional=True)

@app.route('/api/audio/<int:file_id>/info')
def audio_info(file_id):
    """Get audio file technical info"""
    import wave
    import struct
    
    file_data = db.get_file(file_id)
    if not file_data:
        return jsonify({'error': 'File not found'}), 404
    
    filepath = file_data['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Audio file not found on disk'}), 404
    
    info = {
        'filename': file_data['filename'],
        'filepath': filepath,
        'filesize': os.path.getsize(filepath),
    }
    
    try:
        ext = Path(filepath).suffix.lower()
        lay = wav_layout(filepath) if ext in ('.wav', '.wave', '.bwf') else None
        if lay:
            # Header parsed directly: also covers 32/64-bit float and extensible WAVs.
            info['sampleRate'] = lay['rate']
            info['channels'] = lay['channels']
            info['bitDepth'] = lay['bits']
            info['float'] = (lay['fmt'] == 'float')
            info['frames'] = lay['n_frames']
            info['duration'] = lay['n_frames'] / float(lay['rate'])
            info['format'] = ext.replace('.', '').upper()
            threading.Thread(target=_warm_peak_cache, args=(filepath,), daemon=True).start()
        elif ext in ['.wav', '.aiff']:
            with wave.open(filepath, 'rb') as wf:
                info['sampleRate'] = wf.getframerate()
                info['channels'] = wf.getnchannels()
                info['bitDepth'] = wf.getsampwidth() * 8
                info['frames'] = wf.getnframes()
                info['duration'] = wf.getnframes() / wf.getframerate()
                info['format'] = ext.replace('.', '').upper()
            # Warm the waveform peak cache in the background so the first
            # zoom/pan is instant (no effect if already cached).
            threading.Thread(target=_warm_peak_cache, args=(filepath,), daemon=True).start()
        else:
            # For mp3/m4a etc, just show file info
            info['format'] = ext.replace('.', '').upper()
    except Exception as e:
        info['error'] = str(e)

    return jsonify(info)

@app.route('/api/files/durations')
def files_durations():
    """Return {file_id: duration_seconds_or_null} for every file. Reads only the
    WAV/AIFF header (cheap — no audio decode). Non-WAV formats return null."""
    import wave
    out = {}
    for f in db.get_files():
        fp = f.get('filepath')
        dur = None
        try:
            if fp and os.path.exists(fp) and os.path.splitext(fp)[1].lower() in ('.wav', '.wave', '.aiff', '.aif'):
                with wave.open(fp, 'rb') as wf:
                    rate = wf.getframerate()
                    if rate:
                        dur = wf.getnframes() / rate
        except Exception:
            dur = None
        out[str(f['id'])] = dur
    return jsonify(out)

# =====================================================================
# Waveform peak cache v2 (multichannel min / max / RMS)
# =====================================================================
# Each WAV is summarised ONCE into fixed-size buckets of `spb` frames, per
# channel: [min, max, mean-square]. The summary is cached to disk (keyed by
# path+mtime+size+version) and every zoom/pan of the overview is served by
# re-aggregating that array. When the view is zoomed in past the cache's
# resolution, the requested window is read straight from the file (memmap),
# so detail keeps sharpening down to individual samples.

PEAK_CACHE_VERSION = 'v2'
PEAK_MIN_SPB = 256                  # finest cache resolution (frames per bucket)
PEAK_MAX_BUCKETS = 400000           # cap cache length for very long files
WAVEFORM_MAX_BARS = 8000
_WAV_EXTS = ('.wav', '.wave', '.bwf')
_peak_mem = {}                      # key -> dict(summary, spb), small in-memory LRU
_peak_mem_order = []
_peak_locks = {}                    # key -> Lock, so we build each file only once
_peak_locks_guard = threading.Lock()


def _peak_cache_dir():
    d = os.path.expanduser('~/.earmark/peakcache')
    os.makedirs(d, exist_ok=True)
    return d


def _peak_key(filepath):
    st = os.stat(filepath)
    raw = f"{os.path.abspath(filepath)}:{st.st_mtime_ns}:{st.st_size}:{PEAK_CACHE_VERSION}:{PEAK_MIN_SPB}:{PEAK_MAX_BUCKETS}"
    return hashlib.md5(raw.encode()).hexdigest()


def _peak_lock_for(key):
    with _peak_locks_guard:
        lk = _peak_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _peak_locks[key] = lk
        return lk


def _remember_peaks(key, entry):
    _peak_mem[key] = entry
    if key in _peak_mem_order:
        _peak_mem_order.remove(key)
    _peak_mem_order.append(key)
    while len(_peak_mem_order) > 8:
        old = _peak_mem_order.pop(0)
        _peak_mem.pop(old, None)


def wav_layout(filepath):
    """Parse a RIFF/WAVE header without decoding audio.
    Supports PCM 8/16/24/32-bit, IEEE float 32/64-bit and WAVE_FORMAT_EXTENSIBLE.
    Returns dict(fmt='pcm'|'float', channels, rate, bits, block_align,
    data_offset, n_frames) or None if the file can't be understood."""
    import struct as _st
    try:
        with open(filepath, 'rb') as fh:
            head = fh.read(12)
            if len(head) < 12 or head[0:4] != b'RIFF' or head[8:12] != b'WAVE':
                return None
            file_size = os.path.getsize(filepath)
            fmt = None
            data_offset = data_size = None
            pos = 12
            while pos + 8 <= file_size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[0:4]
                csize = _st.unpack('<I', hdr[4:8])[0]
                if cid == b'fmt ':
                    body = fh.read(min(csize, 64))
                    tag, ch, rate, _brate, balign, bits = _st.unpack('<HHIIHH', body[:16])
                    if tag == 0xFFFE and len(body) >= 40:      # WAVE_FORMAT_EXTENSIBLE
                        tag = _st.unpack('<H', body[24:26])[0]
                    fmt = {'tag': tag, 'channels': ch, 'rate': rate,
                           'block_align': balign, 'bits': bits}
                elif cid == b'data':
                    data_offset = pos + 8
                    # Some writers leave 0 / 0xFFFFFFFF while recording; trust the file size.
                    data_size = csize
                    if csize == 0 or csize == 0xFFFFFFFF or data_offset + csize > file_size:
                        data_size = file_size - data_offset
                    break
                pos += 8 + csize + (csize & 1)               # chunks are word-aligned
            if not fmt or data_offset is None or not fmt['channels'] or not fmt['rate']:
                return None
            if fmt['tag'] == 1:
                kind = 'pcm'
                if fmt['bits'] not in (8, 16, 24, 32):
                    return None
            elif fmt['tag'] == 3:
                kind = 'float'
                if fmt['bits'] not in (32, 64):
                    return None
            else:
                return None
            balign = fmt['block_align'] or (fmt['channels'] * fmt['bits'] // 8)
            return {
                'fmt': kind,
                'channels': fmt['channels'],
                'rate': fmt['rate'],
                'bits': fmt['bits'],
                'block_align': balign,
                'data_offset': data_offset,
                'n_frames': data_size // balign,
            }
    except Exception:
        return None


def _decode_frames(lay, raw):
    """Bytes (whole frames) -> float32 array (n_frames, channels) in [-1, 1]."""
    ch = lay['channels']
    bits = lay['bits']
    bps = bits // 8
    n = len(raw) // (ch * bps)
    raw = raw[:n * ch * bps]
    if lay['fmt'] == 'float':
        a = np.frombuffer(raw, dtype='<f4' if bits == 32 else '<f8').astype(np.float32)
    elif bits == 8:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif bits == 16:
        a = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 0x800000, v - 0x1000000, v)
        a = v.astype(np.float32) / 8388608.0
    else:
        a = np.frombuffer(raw, dtype='<i4').astype(np.float32) / 2147483648.0
    return a.reshape(n, ch)


def _read_frames(filepath, lay, start_frame, count):
    """Read `count` frames starting at `start_frame` -> float32 (count, channels)."""
    start_frame = max(0, min(lay['n_frames'], start_frame))
    count = max(0, min(lay['n_frames'] - start_frame, count))
    if count == 0:
        return np.zeros((0, lay['channels']), dtype=np.float32)
    with open(filepath, 'rb') as fh:
        fh.seek(lay['data_offset'] + start_frame * lay['block_align'])
        raw = fh.read(count * lay['block_align'])
    if lay['block_align'] != lay['channels'] * lay['bits'] // 8:
        # Padded frames (rare): strip padding bytes per frame.
        bpf = lay['channels'] * lay['bits'] // 8
        nfr = len(raw) // lay['block_align']
        raw = np.frombuffer(raw[:nfr * lay['block_align']], dtype=np.uint8) \
                .reshape(nfr, lay['block_align'])[:, :bpf].tobytes()
    return _decode_frames(lay, raw)


def _summarise(frames, spb):
    """(n, C) float -> (ceil(n/spb), C, 3) [min, max, mean-square] per bucket."""
    n, C = frames.shape
    if n == 0:
        return np.zeros((0, C, 3), dtype=np.float32)
    nb = -(-n // spb)
    pad = nb * spb - n
    if pad:
        # Pad with the last bucket's own values so min/max are unaffected;
        # mean-square of the partial bucket is fixed up below.
        frames = np.concatenate([frames, np.repeat(frames[-1:], pad, axis=0)])
    w = frames.reshape(nb, spb, C)
    out = np.empty((nb, C, 3), dtype=np.float32)
    out[:, :, 0] = w.min(axis=1)
    out[:, :, 1] = w.max(axis=1)
    sq = (w.astype(np.float64) ** 2)
    if pad:
        out[:-1, :, 2] = sq[:-1].mean(axis=1)
        last = frames[(nb - 1) * spb:n] if n > (nb - 1) * spb else frames[-1:]
        out[-1, :, 2] = (last.astype(np.float64) ** 2).mean(axis=0)
    else:
        out[:, :, 2] = sq.mean(axis=1)
    return out


def _build_peaks(filepath, lay):
    """Stream the file once and reduce it to the v2 summary. Memory bounded to one block."""
    n_frames = lay['n_frames']
    spb = max(PEAK_MIN_SPB, -(-n_frames // PEAK_MAX_BUCKETS))
    block = spb * 2048
    parts = []
    f0 = 0
    while f0 < n_frames:
        cnt = min(block, n_frames - f0)
        parts.append(_summarise(_read_frames(filepath, lay, f0, cnt), spb))
        f0 += cnt
    summary = np.concatenate(parts) if parts else np.zeros((0, lay['channels'], 3), dtype=np.float32)
    return {'summary': summary, 'spb': spb}


def get_cached_peaks(filepath):
    """Return {'summary': (N, C, 3), 'spb': int, 'layout': dict} for a WAV,
    building + caching it on first use. None if unsupported / numpy missing."""
    if np is None:
        return None
    if os.path.splitext(filepath)[1].lower() not in _WAV_EXTS:
        return None
    lay = wav_layout(filepath)
    if not lay or lay['n_frames'] <= 0:
        return None
    try:
        key = _peak_key(filepath)
    except OSError:
        return None
    cached = _peak_mem.get(key)
    if cached is not None:
        return dict(cached, layout=lay)
    disk = os.path.join(_peak_cache_dir(), key + '.npz')
    lock = _peak_lock_for(key)
    with lock:
        cached = _peak_mem.get(key)            # re-check after acquiring the lock
        if cached is not None:
            return dict(cached, layout=lay)
        entry = None
        if os.path.exists(disk):
            try:
                z = np.load(disk)
                entry = {'summary': z['summary'], 'spb': int(z['spb'])}
            except Exception:
                entry = None
        if entry is None:
            try:
                entry = _build_peaks(filepath, lay)
            except Exception as e:
                print('Peak build failed for', filepath, e)
                return None
            try:
                np.savez(disk, summary=entry['summary'], spb=np.array(entry['spb']))
            except Exception:
                pass
        _remember_peaks(key, entry)
        return dict(entry, layout=lay)


def _aggregate(summary, bars):
    """Re-aggregate an (M, C, 3) summary down to at most `bars` columns."""
    M = len(summary)
    if M <= bars:
        return summary
    edges = np.linspace(0, M, bars + 1).astype(np.int64)
    starts = np.unique(edges[:-1])
    counts = np.diff(np.append(starts, M)).astype(np.float64)
    out = np.empty((len(starts), summary.shape[1], 3), dtype=np.float32)
    out[:, :, 0] = np.minimum.reduceat(summary[:, :, 0], starts, axis=0)
    out[:, :, 1] = np.maximum.reduceat(summary[:, :, 1], starts, axis=0)
    out[:, :, 2] = np.add.reduceat(summary[:, :, 2].astype(np.float64), starts, axis=0) / counts[:, None]
    return out


def _wav_duration(filepath):
    lay = wav_layout(filepath)
    if lay:
        return lay['n_frames'] / float(lay['rate'])
    import wave
    try:
        with wave.open(filepath, 'rb') as wf:
            r = wf.getframerate()
            return wf.getnframes() / r if r else 0
    except Exception:
        return 0


def _warm_peak_cache(filepath):
    """Build the peak cache in the background (best-effort)."""
    try:
        get_cached_peaks(filepath)
    except Exception:
        pass


def _pack_channels(agg):
    """(B, C, 3) [min,max,meansq] -> [{'min':[], 'max':[], 'rms':[]}, ...]"""
    chans = []
    for c in range(agg.shape[1]):
        chans.append({
            'min': np.round(agg[:, c, 0].astype(np.float64), 4).tolist(),
            'max': np.round(agg[:, c, 1].astype(np.float64), 4).tolist(),
            'rms': np.round(np.sqrt(np.maximum(agg[:, c, 2].astype(np.float64), 0)), 4).tolist(),
        })
    return chans


# ---------------------------------------------------------------------------
# Spectrogram (WAV): computed on demand for the visible window only, so it
# costs nothing unless the spectrogram view is switched on. Recent results are
# kept in memory; nothing is written to disk.
# ---------------------------------------------------------------------------
SPECTRO_MAX_COLS = 4096
SPECTRO_MAX_ROWS = 1024
SPECTRO_FLOOR_DB = -100.0
SPECTRO_CHUNK = 256                 # columns processed per FFT batch (bounds memory)
_spectro_mem = {}
_spectro_order = []
_spectro_lock = threading.Lock()


def _spectro_window_len(rate):
    """Analysis window ≈ 46 ms, as a power of two (2048 at 44.1/48 kHz)."""
    L = 1024
    while L < rate * 0.046 and L < 16384:
        L *= 2
    return L


class _FrameReader(object):
    """Reads mono analysis frames from a WAV with one open file handle."""
    def __init__(self, filepath, lay):
        self.lay = lay
        self.fh = open(filepath, 'rb')
        self.simple = lay['block_align'] == lay['channels'] * lay['bits'] // 8
        self.path = filepath

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass

    def read(self, start, count):
        lay = self.lay
        n = lay['n_frames']
        out = np.zeros(count, dtype=np.float32)
        a, b = max(0, start), min(n, start + count)
        if b <= a:
            return out
        if self.simple:
            self.fh.seek(lay['data_offset'] + a * lay['block_align'])
            fr = _decode_frames(lay, self.fh.read((b - a) * lay['block_align']))
        else:
            fr = _read_frames(self.path, lay, a, b - a)
        mono = fr.mean(axis=1) if fr.shape[1] > 1 else fr[:, 0]
        out[a - start:a - start + len(mono)] = mono
        return out


def _spectro_compute(filepath, lay, sf, ef, cols, rows):
    """Return (cols, rows) uint8 image of the window [sf, ef) in frames."""
    rate = float(lay['rate'])
    L = _spectro_window_len(rate)
    nfft = L * 2                                  # zero-padded: smoother spectrum between bins
    win = np.hanning(L).astype(np.float32)
    scale = 2.0 / win.sum()
    hop = (ef - sf) / float(cols)
    # Zoomed out: several FFTs per column, power-averaged, so nothing between
    # columns is skipped. Bounded so a request stays well under a second.
    k = int(max(1, min(8, np.floor(hop / L))))
    while k > 1 and cols * k > 12000:
        k -= 1

    fmax = rate / 2.0
    fmin = min(40.0, fmax / 8.0)
    bin_hz = rate / nfft
    nb = nfft // 2 + 1
    # Log-spaced row edges. A row spanning >= 2 FFT bins takes the loudest bin
    # in it; a row narrower than a bin is interpolated at its centre frequency.
    edges = np.geomspace(fmin, fmax, rows + 1)
    lo = np.clip(np.floor(edges[:-1] / bin_hz).astype(int), 0, nb - 1)
    hi = np.clip(np.ceil(edges[1:] / bin_hz).astype(int), 1, nb)
    wide = (hi - lo) >= 2
    centre = np.sqrt(edges[:-1] * edges[1:]) / bin_hz
    i0 = np.clip(np.floor(centre).astype(int), 0, nb - 2)
    frac = (centre - i0).astype(np.float32)

    reader = _FrameReader(filepath, lay)
    try:
        span_lo = int(sf - L)
        span_hi = int(ef + L)
        whole = None
        if (span_hi - span_lo) * lay['block_align'] <= 96 * 1024 * 1024:
            whole = reader.read(span_lo, span_hi - span_lo)
        out = np.empty((cols, rows), dtype=np.uint8)
        for c0 in range(0, cols, SPECTRO_CHUNK):
            c1 = min(cols, c0 + SPECTRO_CHUNK)
            ncol = c1 - c0
            frames = np.empty((ncol * k, L), dtype=np.float32)
            for ci in range(ncol):
                col_start = sf + (c0 + ci) * hop
                for j in range(k):
                    centre_f = col_start + (j + 0.5) * hop / k
                    a = int(round(centre_f)) - L // 2
                    if whole is not None:
                        s = a - span_lo
                        frames[ci * k + j] = whole[s:s + L] if 0 <= s and s + L <= len(whole) else reader.read(a, L)
                    else:
                        frames[ci * k + j] = reader.read(a, L)
            spec = np.abs(np.fft.rfft(frames * win, n=nfft, axis=1)) * scale
            power = (spec * spec).reshape(ncol, k, nb).mean(axis=1)
            db_ = 10.0 * np.log10(np.maximum(power, 1e-20))
            peak = np.maximum.reduceat(db_, lo, axis=1)
            interp = db_[:, i0] * (1.0 - frac) + db_[:, i0 + 1] * frac
            vals = np.where(wide[None, :], peak, interp)
            norm = (vals - SPECTRO_FLOOR_DB) / (0.0 - SPECTRO_FLOOR_DB)
            out[c0:c1] = (np.clip(norm, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        return out, fmin, fmax
    finally:
        reader.close()


@app.route('/api/audio/<int:file_id>/spectrogram')
def audio_spectrogram(file_id):
    """Spectrogram of the window [start, end] (fractions of the file).

    Query params: cols, rows (≈ device pixels of the lane), start, end.
    Returns JSON: cols, rows, window, fmin, fmax, data (base64 uint8,
    column-major: data[col * rows + row], row 0 = lowest frequency).
    """
    import base64
    if np is None:
        return jsonify({'error': 'Spectrogram needs numpy'}), 501
    file_data = db.get_file(file_id)
    if not file_data:
        return jsonify({'error': 'File not found'}), 404
    filepath = file_data['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Audio file not found on disk'}), 404
    if os.path.splitext(filepath)[1].lower() not in _WAV_EXTS:
        return jsonify({'error': 'Spectrogram is available for WAV files'}), 415
    lay = wav_layout(filepath)
    if not lay or lay['n_frames'] <= 0:
        return jsonify({'error': 'Unsupported WAV format'}), 415
    try:
        cols = max(16, min(SPECTRO_MAX_COLS, int(request.args.get('cols', 1200))))
        rows = max(32, min(SPECTRO_MAX_ROWS, int(request.args.get('rows', 256))))
        start_frac = max(0.0, min(1.0, float(request.args.get('start', 0))))
        end_frac = max(0.0, min(1.0, float(request.args.get('end', 1))))
    except (TypeError, ValueError):
        return jsonify({'error': 'Bad parameters'}), 400
    if end_frac <= start_frac:
        end_frac = min(1.0, start_frac + 1e-6)

    n = lay['n_frames']
    sf = int(np.floor(start_frac * n))
    ef = max(sf + 1, min(n, int(np.ceil(end_frac * n))))
    try:
        st = os.stat(filepath)
        key = (filepath, st.st_mtime_ns, st.st_size, sf, ef, cols, rows)
    except OSError:
        key = None
    if key is not None:
        with _spectro_lock:
            hit = _spectro_mem.get(key)
        if hit is not None:
            return jsonify(hit)
    try:
        img, fmin, fmax = _spectro_compute(filepath, lay, sf, ef, cols, rows)
    except Exception as e:
        return jsonify({'error': 'Could not read audio: %s' % e}), 500
    result = {
        'cols': cols, 'rows': rows,
        'window': [sf / float(n), ef / float(n)],
        'fmin': fmin, 'fmax': fmax,
        'data': base64.b64encode(img.tobytes()).decode('ascii'),
    }
    if key is not None:
        with _spectro_lock:
            _spectro_mem[key] = result
            _spectro_order.append(key)
            while len(_spectro_order) > 12:
                _spectro_mem.pop(_spectro_order.pop(0), None)
    return jsonify(result)

@app.route('/api/audio/<int:file_id>/waveform')
def audio_waveform(file_id):
    """Waveform data for the visible window.

    Query params:
        bars (int):            columns wanted (≈ canvas pixels), max WAVEFORM_MAX_BARS
        start, end (0..1):     window as a fraction of the file

    Returns JSON:
        mode:      'envelope' (per-column min/max/rms) or 'samples' (raw samples)
        channels:  [{min, max, rms}] per channel   (envelope mode)
        samples:   [[...], ...] per channel         (samples mode)
        window:    [start, end] fractions actually covered (frame-aligned)
        duration, sampleRate, frames, nChannels
    """
    file_data = db.get_file(file_id)
    if not file_data:
        return jsonify({'error': 'File not found'}), 404

    filepath = file_data['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'Audio file not found on disk'}), 404

    try:
        num_bars = int(request.args.get('bars', 1500))
    except (TypeError, ValueError):
        num_bars = 1500
    num_bars = max(16, min(WAVEFORM_MAX_BARS, num_bars))
    try:
        start_frac = max(0.0, min(1.0, float(request.args.get('start', 0))))
        end_frac   = max(0.0, min(1.0, float(request.args.get('end',   1))))
    except (TypeError, ValueError):
        start_frac, end_frac = 0.0, 1.0
    if end_frac <= start_frac:
        end_frac = min(1.0, start_frac + 1e-9)

    cache = get_cached_peaks(filepath)
    if cache is not None:
        lay = cache['layout']
        n = lay['n_frames']
        spb = cache['spb']
        summary = cache['summary']
        sf = int(np.floor(start_frac * n))
        ef = int(np.ceil(end_frac * n))
        ef = max(sf + 1, min(n, ef))
        count = ef - sf
        base = {
            'duration': n / float(lay['rate']),
            'sampleRate': lay['rate'],
            'frames': n,
            'nChannels': lay['channels'],
        }
        try:
            if count <= num_bars:
                # Deep zoom: send the actual samples.
                fr = _read_frames(filepath, lay, sf, count)
                base.update({
                    'mode': 'samples',
                    'samples': [np.round(fr[:, c].astype(np.float64), 5).tolist() for c in range(fr.shape[1])],
                    'window': [sf / n, (sf + len(fr)) / n],
                })
                return jsonify(base)
            if count / float(num_bars) < spb * 2:
                # Zoomed past the cache's resolution: summarise straight from the file.
                fpb = max(1, -(-count // num_bars))
                fr = _read_frames(filepath, lay, sf, count)
                agg = _summarise(fr, fpb)
                base.update({'mode': 'envelope', 'channels': _pack_channels(agg),
                             'window': [sf / n, (sf + len(fr)) / n]})
                return jsonify(base)
            # Overview: re-aggregate the cached summary.
            bs = sf // spb
            be = min(len(summary), -(-ef // spb))
            be = max(bs + 1, be)
            agg = _aggregate(summary[bs:be], num_bars)
            base.update({'mode': 'envelope', 'channels': _pack_channels(agg),
                         'window': [bs * spb / float(n), min(n, be * spb) / float(n)]})
            return jsonify(base)
        except Exception as e:
            print('Waveform error:', e)
            # fall through to the legacy decoder

    # ---- Legacy fallback (no numpy / unusual file): left channel, min/max only ----
    import wave
    import struct
    try:
        with wave.open(filepath, 'rb') as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            start_frame = int(start_frac * n_frames)
            end_frame   = int(end_frac   * n_frames)
            n_window    = max(1, end_frame - start_frame)
            wf.setpos(start_frame)
            raw = wf.readframes(n_window)
            n_window = len(raw) // (sampwidth * n_channels)
            raw = raw[:n_window * sampwidth * n_channels]
            if sampwidth == 1:
                samples = [s - 128 for s in raw]
                max_val = 128
            elif sampwidth == 2:
                samples = struct.unpack(f'<{n_window * n_channels}h', raw)
                max_val = 32768
            elif sampwidth == 3:
                samples = []
                for i in range(0, len(raw), 3):
                    val = raw[i] | (raw[i+1] << 8) | (raw[i+2] << 16)
                    if val >= 0x800000:
                        val -= 0x1000000
                    samples.append(val)
                max_val = 8388608
            elif sampwidth == 4:
                samples = struct.unpack(f'<{n_window * n_channels}i', raw)
                max_val = 2147483648
            else:
                return jsonify({'error': 'Unsupported bit depth'}), 400
            if n_channels > 1:
                samples = samples[::n_channels]
            chunk_size = max(1, len(samples) // num_bars)
            mins, maxs = [], []
            for i in range(0, len(samples), chunk_size):
                chunk = samples[i:i + chunk_size]
                if not chunk:
                    continue
                mins.append(round(min(chunk) / max_val, 4))
                maxs.append(round(max(chunk) / max_val, 4))
            return jsonify({
                'mode': 'envelope',
                'channels': [{'min': mins, 'max': maxs, 'rms': None}],
                'duration': n_frames / framerate,
                'sampleRate': framerate,
                'frames': n_frames,
                'nChannels': 1,
                'window': [start_frame / n_frames, (start_frame + n_window) / n_frames] if n_frames else [0, 1],
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/datalink/content')
def datalink_content():
    """Return the text contents of a linked data file for display in the viewer modal.

    Query params:
        path: absolute filesystem path to the linked file.

    Returns JSON with:
        ok (bool), content (str), truncated (bool),
        size_limit (int, bytes), full_size (int, bytes),
        error (str) when ok is false.
    """
    path = request.args.get('path', '')
    if not path:
        return jsonify({'ok': False, 'error': 'No path provided.'})

    # Basic existence / type checks
    if not os.path.exists(path):
        return jsonify({'ok': False, 'error': 'File not found: ' + path})
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'Path is not a regular file.'})

    # Only serve known text-like extensions (matches the linker's whitelist).
    text_extensions = {'.txt', '.textgrid', '.csv', '.md', '.json', '.eaf', '.xml',
                       '.tsv', '.log', '.yaml', '.yml', '.ini', '.cfg'}
    ext = os.path.splitext(path)[1].lower()
    if ext not in text_extensions:
        return jsonify({'ok': False,
                        'error': 'Unsupported file type for in-app viewer: ' + (ext or '(no extension)')
                                 + '. Open in an external editor instead.'})

    SIZE_LIMIT = 2 * 1024 * 1024  # 2 MB read cap for the viewer

    try:
        full_size = os.path.getsize(path)
    except OSError as e:
        return jsonify({'ok': False, 'error': 'Cannot stat file: ' + str(e)})

    # Read up to SIZE_LIMIT bytes, trying a few encodings for robustness.
    raw = None
    try:
        with open(path, 'rb') as f:
            raw = f.read(SIZE_LIMIT)
    except OSError as e:
        return jsonify({'ok': False, 'error': 'Cannot read file: ' + str(e)})

    content = None
    for encoding in ('utf-8', 'utf-16', 'latin-1', 'cp1252'):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        # Last resort: decode with replacement so we never 500 on weird encodings.
        content = raw.decode('utf-8', errors='replace')

    return jsonify({
        'ok': True,
        'content': content,
        'truncated': full_size > SIZE_LIMIT,
        'size_limit': SIZE_LIMIT,
        'full_size': full_size,
    })

# Database management routes
current_db_path = os.path.abspath(DB_PATH)

# --- Persistent app settings (remembers the last-opened database) -----------
def _settings_path():
    d = os.path.expanduser('~/.earmark')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'settings.json')

def _load_settings():
    try:
        with open(_settings_path(), 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings(updates):
    try:
        data = _load_settings()
        data.update(updates)
        with open(_settings_path(), 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

def _remember_last_db(path):
    _save_settings({'last_db': os.path.abspath(path)})

@app.route('/api/settings/shortcuts', methods=['GET', 'POST'])
def shortcut_settings():
    """Custom keyboard shortcuts: only the actions you changed are stored."""
    if request.method == 'POST':
        data = request.json or {}
        ov = data.get('overrides', {})
        if not isinstance(ov, dict):
            return jsonify({'error': 'overrides must be an object'}), 400
        clean = {}
        for k, v in ov.items():
            if isinstance(k, str) and isinstance(v, list):
                clean[k[:40]] = [str(x)[:40] for x in v[:4]]
        _save_settings({'shortcuts': clean})
        return jsonify({'success': True})
    return jsonify({'overrides': _load_settings().get('shortcuts', {})})

@app.route('/api/database/current')
def get_current_database():
    global current_db_path
    return jsonify({'path': current_db_path, 'name': os.path.basename(current_db_path)})

@app.route('/api/database/new', methods=['POST'])
def new_database():
    global db, current_db_path
    
    name = request.json.get('name', 'new.db')
    if not name.endswith('.db'):
        name += '.db'
    
    new_path = _resolve_db_path(name)
    
    if os.path.exists(new_path):
        return jsonify({'success': False, 'error': 'Database already exists'})
    
    # Create new database
    db = EarmarkDB(new_path)
    current_db_path = new_path
    _remember_last_db(new_path)

    return jsonify({'success': True, 'path': new_path})

@app.route('/api/database/open', methods=['POST'])
def open_database():
    global db, current_db_path
    
    path = request.json.get('path', '')
    
    # Bare names: look in DATA_DIR first, then the launch folder (old location)
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        candidate = _resolve_db_path(path)
        path = candidate if os.path.exists(candidate) else os.path.abspath(path)
    
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'Database not found: ' + path})
    
    db = EarmarkDB(path)
    current_db_path = path
    _remember_last_db(path)

    return jsonify({'success': True, 'path': path})

@app.route('/api/database/saveas', methods=['POST'])
def save_database_as():
    global db, current_db_path
    import shutil
    
    name = request.json.get('name', 'copy.db')
    if not name.endswith('.db'):
        name += '.db'
    
    new_path = _resolve_db_path(name)
    
    if new_path == current_db_path:
        return jsonify({'success': True, 'path': current_db_path})
    
    # Copy current database to new location
    shutil.copy2(current_db_path, new_path)
    
    # Open the new database
    db = EarmarkDB(new_path)
    current_db_path = new_path
    _remember_last_db(new_path)

    return jsonify({'success': True, 'path': new_path})

@app.route('/api/export/csv')
def export_csv():
    """Export all data as CSV"""
    import csv
    import io
    
    output = io.StringIO()
    
    # Get all fields
    fields = db.get_fields()
    field_names = [f['name'] for f in fields]
    
    # CSV header
    header = ['filename', 'filepath', 'tags'] + field_names
    writer = csv.writer(output)
    writer.writerow(header)
    
    # Write data
    for file in db.get_files():
        file_detail = db.get_file(file['id'])
        row = [
            file_detail['filename'],
            file_detail['filepath'],
            ';'.join(file_detail.get('tags', []))
        ]
        
        # Add field values
        for field in fields:
            value = file_detail['fields'].get(field['name'], '')
            # For markers, format as readable string
            if field['type'] == 'markers' and value:
                try:
                    markers = json.loads(value)
                    value = '; '.join([f"{m['label']} [{m['start']}-{m['end']}]" for m in markers])
                except:
                    pass
            row.append(value)
        
        writer.writerow(row)
    
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=earmark_export.csv'}
    )


def _xlsx_col_letter(n):
    """1-based column index -> spreadsheet letter (1->A, 27->AA)."""
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xe(s):
    """Escape text for XML."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def build_xlsx(headers, rows, widths=None):
    """Build a minimal single-sheet .xlsx (no external deps).
    `rows` is a list of lists; numeric (int/float, non-bool) cells become real
    numbers, everything else an inline string. Header row is bold + frozen,
    with an autofilter over the table. Returns the file as bytes."""
    import io, zipfile

    def cell_xml(ref, val, style=None):
        st = f' s="{style}"' if style else ''
        if isinstance(val, bool):
            val = str(val)
        if isinstance(val, (int, float)):
            return f'<c r="{ref}"{st}><v>{val}</v></c>'
        text = _xe(val if val is not None else '')
        return f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    n_cols = len(headers)
    last_col = _xlsx_col_letter(n_cols)
    n_rows = len(rows) + 1

    # Header row (bold style index 1)
    parts = ['<row r="1">']
    for ci, h in enumerate(headers, start=1):
        parts.append(cell_xml(f'{_xlsx_col_letter(ci)}1', h, style=1))
    parts.append('</row>')
    # Data rows
    for ri, row in enumerate(rows, start=2):
        parts.append(f'<row r="{ri}">')
        for ci in range(1, n_cols + 1):
            val = row[ci - 1] if ci - 1 < len(row) else ''
            parts.append(cell_xml(f'{_xlsx_col_letter(ci)}{ri}', val))
        parts.append('</row>')
    sheet_data = ''.join(parts)

    if widths is None:
        widths = [18] * n_cols
    cols_xml = ''.join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths, start=1)
    )

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols_xml}</cols>'
        f'<sheetData>{sheet_data}</sheetData>'
        f'<autoFilter ref="A1:{last_col}{n_rows}"/>'
        '</worksheet>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Earmark" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


def _export_xlsx_bytes():
    """Build the .xlsx export (bold frozen header, autofilter, numeric columns
    kept numeric; mirrors the CSV export plus a Group column). Returns bytes."""
    fields = db.get_fields()
    group_names = {g['id']: g['name'] for g in db.get_groups()}

    headers = ['filename', 'filepath', 'group', 'tags'] + [f['name'] for f in fields]

    rows = []
    for file in db.get_files():
        fd = db.get_file(file['id'])
        row = [
            fd['filename'],
            fd['filepath'],
            group_names.get(fd.get('group_id'), ''),
            ';'.join(fd.get('tags', [])),
        ]
        for field in fields:
            value = fd['fields'].get(field['name'], '')
            if field['type'] == 'markers' and value:
                try:
                    markers = json.loads(value)
                    value = '; '.join([f"{m['label']} [{m['start']}-{m['end']}]" for m in markers])
                except Exception:
                    pass
            elif field['type'] == 'parameter' and value not in ('', None):
                # Keep numeric so Excel can sort/filter/sum it.
                try:
                    value = float(value)
                    if value.is_integer():
                        value = int(value)
                except (TypeError, ValueError):
                    pass
            row.append(value)
        rows.append(row)

    widths = [28, 50, 16, 22] + [16] * len(fields)
    return build_xlsx(headers, rows, widths)


@app.route('/api/export/xlsx')
def export_xlsx():
    """Download the .xlsx (works in a normal browser)."""
    return Response(
        _export_xlsx_bytes(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=earmark_export.xlsx'}
    )


@app.route('/api/export/xlsx/save', methods=['POST'])
def export_xlsx_save():
    """Write the .xlsx to disk next to the current database and reveal it in
    Finder. This is the reliable path for the desktop (pywebview) window, which
    can't perform browser-style downloads."""
    try:
        data = _export_xlsx_bytes()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return _save_export_next_to_db(data, '_export.xlsx')


def _save_export_next_to_db(data, suffix):
    """Write export bytes next to the current database as <db name><suffix>,
    reveal the file in Finder / Explorer, and report where it went."""
    out_dir = os.path.dirname(os.path.abspath(current_db_path)) or '.'
    base = os.path.splitext(os.path.basename(current_db_path))[0] or 'earmark'
    out_path = os.path.join(out_dir, base + suffix)
    try:
        with open(out_path, 'wb') as f:
            f.write(data)
    except Exception as e:
        return jsonify({'success': False, 'error': 'Could not write file: ' + str(e)}), 500

    revealed = False
    try:
        import subprocess
        if sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', out_path])
            revealed = True
        elif sys.platform.startswith('win'):
            subprocess.Popen(['explorer', '/select,', out_path])
            revealed = True
    except Exception:
        pass

    return jsonify({'success': True, 'path': out_path, 'revealed': revealed})


@app.route('/api/export/save', methods=['POST'])
def export_save():
    """Save the JSON / Max / CSV export as a file next to the current database.
    (The desktop window can't do browser-style downloads.)"""
    kind = (request.json or {}).get('kind', '')
    try:
        if kind == 'json':
            data, suffix = export_data().get_data(), '_export.json'
        elif kind == 'max':
            data, suffix = export_for_max().get_data(), '_max.json'
        elif kind == 'csv':
            data, suffix = export_csv().get_data(), '_export.csv'
        else:
            return jsonify({'success': False, 'error': 'Unknown export: ' + str(kind)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return _save_export_next_to_db(data, suffix)

@app.route('/api/export')
def export_data():
    """Export all data as JSON for MaxMSP or backup"""
    files_data = []
    for file in db.get_files():
        file_detail = db.get_file(file['id'])
        files_data.append(file_detail)
    
    export = {
        'fields': db.get_fields(),
        'files': files_data,
        'exported_at': datetime.now().isoformat()
    }
    
    return jsonify(export)

def time_to_seconds(time_str):
    """Convert HH:MM:SS to seconds"""
    try:
        parts = time_str.split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(time_str)
    except:
        return 0

@app.route('/api/export/max')
def export_for_max():
    """Export all data in Max-friendly format.
    - Flat structure (no nested 'fields' object)
    - Times in seconds
    - Index for polybuffer~ reference
    """
    files_data = []
    all_fields = db.get_fields()
    
    for idx, file in enumerate(db.get_files()):
        file_detail = db.get_file(file['id'])
        
        # Build flat file object
        file_obj = {
            'index': idx,
            'filename': file_detail['filename'],
            'filepath': file_detail['filepath'],
            'tags': file_detail['tags']
        }
        
        # Add all field values directly (flat, not nested)
        for field in all_fields:
            field_name = field['name']
            raw_value = file_detail['fields'].get(field_name, '')
            
            if field['type'] == 'parameter':
                # Convert to number
                try:
                    file_obj[field_name] = float(raw_value) if raw_value else None
                except:
                    file_obj[field_name] = None
                    
            elif field['type'] == 'markers':
                # Parse markers and convert times to seconds
                markers = []
                try:
                    raw_markers = json.loads(raw_value) if raw_value else []
                    for m in raw_markers:
                        markers.append({
                            'label': m.get('label', ''),
                            'start_sec': time_to_seconds(m.get('start', '0')),
                            'end_sec': time_to_seconds(m.get('end', '0'))
                        })
                    # Sort by start time
                    markers.sort(key=lambda x: x['start_sec'])
                except:
                    markers = []
                file_obj[field_name] = markers
                
            elif field['type'] == 'tags':
                # Parse field-specific tags
                try:
                    file_obj[field_name] = json.loads(raw_value) if raw_value else []
                except:
                    file_obj[field_name] = []
                    
            elif field['type'] == 'choice':
                file_obj[field_name] = raw_value if raw_value else None
                
            elif field['type'] == 'text':
                file_obj[field_name] = raw_value if raw_value else None
                
            elif field['type'] == 'datalink':
                file_obj[field_name] = raw_value if raw_value else None
                # Also include indexed content if available
                content_key = field_name + '_content'
                if content_key in file_detail['fields']:
                    file_obj[field_name + '_content'] = file_detail['fields'][content_key]
        
        files_data.append(file_obj)
    
    # Build field info (types, ranges, choices)
    fields_info = {}
    for field in all_fields:
        fields_info[field['name']] = {
            'type': field['type']
        }
        if field['type'] == 'parameter':
            fields_info[field['name']]['min'] = field['min']
            fields_info[field['name']]['max'] = field['max']
        elif field['type'] == 'choice':
            fields_info[field['name']]['choices'] = field['choices']
    
    export = {
        'files': files_data,
        'fields': fields_info,
        'file_count': len(files_data),
        'exported_at': datetime.now().isoformat()
    }
    
    return jsonify(export)


@app.route('/api/max/search')
def max_search():
    """
    Search endpoint for Max/MSP.
    
    Query parameters:
        text=<word>              Search in all text/datalink content
        tag=<tag>                Search for files with this tag
        <fieldname>=<value>      Exact match for choice/text fields
        <fieldname>_min=<n>      Parameter >= n
        <fieldname>_max=<n>      Parameter <= n
        <fieldname>_contains=<s> Field contains string
        random=1                 Return one random result
        limit=<n>                Max number of results
    
    Returns flat array of matching files with index, filename, filepath, and all metadata.
    
    Examples:
        /api/max/search?text=mama
        /api/max/search?H_min=10&H_max=15
        /api/max/search?tag=happy&random=1
        /api/max/search?Language=English&H_min=5
    """
    import random as rand
    
    all_fields = db.get_fields()
    field_map = {f['name']: f for f in all_fields}
    
    # Get all files with details
    all_files = []
    for idx, file in enumerate(db.get_files()):
        file_detail = db.get_file(file['id'])
        
        file_obj = {
            'index': idx,
            'filename': file_detail['filename'],
            'filepath': file_detail['filepath'],
            'tags': file_detail['tags']
        }
        
        # Add field values
        for field in all_fields:
            field_name = field['name']
            raw_value = file_detail['fields'].get(field_name, '')
            
            if field['type'] == 'parameter':
                try:
                    file_obj[field_name] = float(raw_value) if raw_value else None
                except:
                    file_obj[field_name] = None
            elif field['type'] == 'markers':
                markers = []
                try:
                    raw_markers = json.loads(raw_value) if raw_value else []
                    for m in raw_markers:
                        markers.append({
                            'label': m.get('label', ''),
                            'start_sec': time_to_seconds(m.get('start', '0')),
                            'end_sec': time_to_seconds(m.get('end', '0'))
                        })
                    markers.sort(key=lambda x: x['start_sec'])
                except:
                    pass
                file_obj[field_name] = markers
            elif field['type'] == 'tags':
                try:
                    file_obj[field_name] = json.loads(raw_value) if raw_value else []
                except:
                    file_obj[field_name] = []
            elif field['type'] == 'datalink':
                file_obj[field_name] = raw_value if raw_value else None
                content_key = field_name + '_content'
                if content_key in file_detail['fields']:
                    file_obj[field_name + '_content'] = file_detail['fields'][content_key]
            else:
                file_obj[field_name] = raw_value if raw_value else None
        
        all_files.append(file_obj)
    
    # Now filter based on query params
    results = all_files
    
    # Text search (searches all text and datalink content)
    text_query = request.args.get('text', '').lower()
    if text_query:
        filtered = []
        for f in results:
            found = False
            for key, val in f.items():
                if key.endswith('_content') and val and text_query in val.lower():
                    found = True
                    break
                if isinstance(val, str) and text_query in val.lower():
                    found = True
                    break
            if found:
                filtered.append(f)
        results = filtered
    
    # Tag search
    tag_query = request.args.get('tag', '').lower()
    if tag_query:
        results = [f for f in results if any(tag_query in t.lower() for t in f.get('tags', []))]
    
    # Field-specific queries
    for param, value in request.args.items():
        if param in ['text', 'tag', 'random', 'limit']:
            continue
        
        # Handle _min, _max, _contains suffixes
        if param.endswith('_min'):
            field_name = param[:-4]
            if field_name in field_map:
                try:
                    min_val = float(value)
                    results = [f for f in results if f.get(field_name) is not None and f[field_name] >= min_val]
                except:
                    pass
        elif param.endswith('_max'):
            field_name = param[:-4]
            if field_name in field_map:
                try:
                    max_val = float(value)
                    results = [f for f in results if f.get(field_name) is not None and f[field_name] <= max_val]
                except:
                    pass
        elif param.endswith('_contains'):
            field_name = param[:-9]
            if field_name in field_map:
                search_val = value.lower()
                filtered = []
                for f in results:
                    field_val = f.get(field_name)
                    content_val = f.get(field_name + '_content', '')
                    if (field_val and search_val in str(field_val).lower()) or \
                       (content_val and search_val in content_val.lower()):
                        filtered.append(f)
                results = filtered
        elif param in field_map:
            # Exact match
            results = [f for f in results if str(f.get(param, '')).lower() == value.lower()]
    
    # Limit
    limit = request.args.get('limit')
    if limit:
        try:
            results = results[:int(limit)]
        except:
            pass
    
    # Random
    if request.args.get('random') == '1' and results:
        results = [rand.choice(results)]
    
    return jsonify({
        'count': len(results),
        'results': results
    })

@app.route('/api/stats/audio')
def audio_stats():
    """Get statistics about all audio files in the database."""
    import wave
    
    all_files = db.get_files()
    total_files = len(all_files)
    
    durations = []
    sizes = []
    formats = {}
    sample_rates = {}
    bit_depths = {}
    channels_count = {}
    errors = 0
    
    for f in all_files:
        filepath = f['filepath']
        if not os.path.exists(filepath):
            errors += 1
            continue
        
        size = os.path.getsize(filepath)
        sizes.append({'filename': f['filename'], 'size': size})
        
        ext = os.path.splitext(filepath)[1].lower().replace('.', '').upper()
        formats[ext] = formats.get(ext, 0) + 1
        
        try:
            if ext in ['WAV', 'AIFF']:
                with wave.open(filepath, 'rb') as wf:
                    dur = wf.getnframes() / wf.getframerate()
                    sr = wf.getframerate()
                    bd = wf.getsampwidth() * 8
                    ch = wf.getnchannels()
                    
                    durations.append({'filename': f['filename'], 'duration': dur})
                    sample_rates[str(sr)] = sample_rates.get(str(sr), 0) + 1
                    bit_depths[str(bd)] = bit_depths.get(str(bd), 0) + 1
                    channels_count[str(ch)] = channels_count.get(str(ch), 0) + 1
        except Exception:
            errors += 1
    
    result = {
        'total_files': total_files,
        'files_not_found': errors,
        'by_format': [{'format': k, 'count': v} for k, v in sorted(formats.items(), key=lambda x: x[1], reverse=True)]
    }
    
    if durations:
        durations.sort(key=lambda x: x['duration'])
        durs = [d['duration'] for d in durations]
        total_dur = sum(durs)
        mean_dur = total_dur / len(durs)
        
        result.update({
            'total_duration_sec': round(total_dur, 1),
            'total_duration_readable': '%d:%02d:%02d' % (int(total_dur // 3600), int((total_dur % 3600) // 60), int(total_dur % 60)),
            'avg_duration_sec': round(mean_dur, 1),
            'min_duration_sec': round(durs[0], 1),
            'min_duration_file': durations[0]['filename'],
            'max_duration_sec': round(durs[-1], 1),
            'max_duration_file': durations[-1]['filename'],
            'files_with_duration': len(durs)
        })
    
    if sizes:
        sizes.sort(key=lambda x: x['size'])
        total_size = sum(s['size'] for s in sizes)
        result.update({
            'total_size_bytes': total_size,
            'total_size_readable': '%.1f MB' % (total_size / 1048576),
            'avg_size_readable': '%.1f MB' % (total_size / len(sizes) / 1048576),
            'smallest_file': sizes[0]['filename'],
            'smallest_size_readable': '%.1f MB' % (sizes[0]['size'] / 1048576),
            'largest_file': sizes[-1]['filename'],
            'largest_size_readable': '%.1f MB' % (sizes[-1]['size'] / 1048576),
        })
    
    if sample_rates:
        result['by_sample_rate'] = [{'rate': k, 'count': v} for k, v in sorted(sample_rates.items())]
    if bit_depths:
        result['by_bit_depth'] = [{'depth': k, 'count': v} for k, v in sorted(bit_depths.items())]
    if channels_count:
        result['by_channels'] = [{'channels': k, 'count': v} for k, v in sorted(channels_count.items())]
    
    return jsonify(result)


@app.route('/api/file/<int:file_id>/stats')
def file_stats(file_id):
    """Get statistics for a single file's metadata."""
    import re as re_module
    import wave
    
    file_data = db.get_file(file_id)
    if not file_data:
        return jsonify({'error': 'File not found'}), 404
    
    fields = db.get_fields()
    result = {
        'filename': file_data['filename'],
        'filepath': file_data['filepath'],
        'tag_count': len(file_data.get('tags', [])),
        'tags': file_data.get('tags', []),
        'fields': {}
    }
    
    # Audio info
    filepath = file_data['filepath']
    if os.path.exists(filepath):
        result['file_size'] = os.path.getsize(filepath)
        result['file_size_readable'] = '%.1f MB' % (result['file_size'] / 1048576)
        try:
            ext = os.path.splitext(filepath)[1].lower()
            if ext in ['.wav', '.aiff']:
                with wave.open(filepath, 'rb') as wf:
                    result['duration_sec'] = round(wf.getnframes() / wf.getframerate(), 1)
                    result['sample_rate'] = wf.getframerate()
                    result['bit_depth'] = wf.getsampwidth() * 8
                    result['channels'] = wf.getnchannels()
        except Exception:
            pass
    
    # Per-field stats
    for field in fields:
        value = file_data['fields'].get(field['name'], '')
        if not value:
            continue
        
        field_info = {'type': field['type']}
        
        if field['type'] == 'markers':
            try:
                markers = json.loads(value)
                colors = {}
                for m in markers:
                    c = m.get('color', 'Unknown')
                    colors[c] = colors.get(c, 0) + 1
                
                # Word frequency in marker labels
                word_counts = {}
                for m in markers:
                    label = m.get('label', '')
                    words = re_module.findall(r'[a-zA-Z\u00C0-\u00FF\u0100-\u024F]+', label.lower())
                    for w in words:
                        word_counts[w] = word_counts.get(w, 0) + 1
                
                top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:20]
                
                field_info.update({
                    'marker_count': len(markers),
                    'by_color': [{'color': k, 'count': v} for k, v in sorted(colors.items(), key=lambda x: x[1], reverse=True)],
                    'unique_words': len(word_counts),
                    'top_words': [{'word': w, 'count': c} for w, c in top_words]
                })
            except:
                pass
        
        elif field['type'] == 'datalink':
            content = file_data['fields'].get(field['name'] + '_content', '')
            if content:
                word_counts = {}
                words = re_module.findall(r'[a-zA-Z\u00C0-\u00FF\u0100-\u024F]+', content.lower())
                for w in words:
                    word_counts[w] = word_counts.get(w, 0) + 1
                
                top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:20]
                field_info.update({
                    'total_words': len(words),
                    'unique_words': len(word_counts),
                    'top_words': [{'word': w, 'count': c} for w, c in top_words]
                })
        
        elif field['type'] == 'text':
            word_counts = {}
            words = re_module.findall(r'[a-zA-Z\u00C0-\u00FF\u0100-\u024F]+', value.lower())
            for w in words:
                word_counts[w] = word_counts.get(w, 0) + 1
            
            top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:20]
            field_info.update({
                'total_words': len(words),
                'unique_words': len(word_counts),
                'top_words': [{'word': w, 'count': c} for w, c in top_words]
            })
        
        elif field['type'] == 'tags':
            try:
                tags = json.loads(value)
                field_info['tag_count'] = len(tags)
                field_info['tags'] = tags
            except:
                pass
        
        elif field['type'] == 'parameter':
            try:
                field_info['value'] = float(value)
            except:
                pass
        
        elif field['type'] == 'choice':
            field_info['value'] = value
        
        result['fields'][field['name']] = field_info
    
    return jsonify(result)


@app.route('/api/stats/<field_name>')
def field_stats(field_name):
    """Get statistics for a field based on its type."""
    fields = db.get_fields()
    
    # Special case: file-level tags
    if field_name.lower() == 'tags':
        all_files = db.get_files()
        tag_counts = {}
        for f in all_files:
            for tag in f.get('tags', []):
                key = tag.lower()
                if key not in tag_counts:
                    tag_counts[key] = {'tag': tag, 'count': 0}
                tag_counts[key]['count'] += 1
        
        sorted_tags = sorted(tag_counts.values(), key=lambda x: x['count'], reverse=True)
        total_files = len(all_files)
        
        return jsonify({
            'field': 'tags',
            'type': 'file_tags',
            'total_files': total_files,
            'unique_tags': len(sorted_tags),
            'tags': [{'tag': t['tag'], 'count': t['count'], 
                      'percent': round(t['count'] / total_files * 100, 1) if total_files else 0} 
                     for t in sorted_tags]
        })
    
    field = next((f for f in fields if f['name'].lower() == field_name.lower()), None)
    if not field:
        return jsonify({'error': 'Field "' + field_name + '" not found'}), 404
    
    conn = sqlite3.connect(db.db_path)
    c = conn.cursor()
    c.execute('SELECT fv.value, fv.content, f.id, f.filename FROM field_values fv JOIN files f ON f.id = fv.file_id WHERE fv.field_id = ? AND fv.value IS NOT NULL AND fv.value != ""', (field['id'],))
    rows = c.fetchall()
    total_files = c.execute('SELECT COUNT(*) FROM files').fetchone()[0]
    conn.close()
    
    result = {
        'field': field['name'],
        'type': field['type'],
        'total_files': total_files,
        'files_with_value': len(rows),
        'files_without_value': total_files - len(rows)
    }
    
    if field['type'] == 'parameter':
        values = []
        for row in rows:
            try:
                values.append(float(row[0]))
            except (ValueError, TypeError):
                pass
        
        if values:
            values.sort()
            n = len(values)
            mean = sum(values) / n
            median = values[n // 2] if n % 2 == 1 else (values[n // 2 - 1] + values[n // 2]) / 2
            variance = sum((v - mean) ** 2 for v in values) / n
            std_dev = variance ** 0.5
            
            result.update({
                'count': n,
                'min': values[0],
                'max': values[-1],
                'mean': round(mean, 2),
                'median': round(median, 2),
                'std_dev': round(std_dev, 2),
                'sum': round(sum(values), 2)
            })
        else:
            result['count'] = 0
    
    elif field['type'] == 'choice':
        counts = {}
        for row in rows:
            val = row[0]
            counts[val] = counts.get(val, 0) + 1
        
        total = sum(counts.values())
        result['options'] = [
            {'value': k, 'count': v, 'percent': round(v / total * 100, 1) if total else 0}
            for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
        ]
    
    elif field['type'] == 'tags':
        tag_counts = {}
        for row in rows:
            try:
                tags = json.loads(row[0])
                for tag in tags:
                    key = tag.lower()
                    if key not in tag_counts:
                        tag_counts[key] = {'tag': tag, 'count': 0}
                    tag_counts[key]['count'] += 1
            except:
                pass
        
        sorted_tags = sorted(tag_counts.values(), key=lambda x: x['count'], reverse=True)
        result['unique_tags'] = len(sorted_tags)
        result['tags'] = [{'tag': t['tag'], 'count': t['count']} for t in sorted_tags]
    
    elif field['type'] == 'markers':
        total_markers = 0
        color_counts = {}
        files_with_markers = 0
        
        for row in rows:
            try:
                markers = json.loads(row[0])
                if markers:
                    files_with_markers += 1
                    total_markers += len(markers)
                    for m in markers:
                        color = m.get('color', 'Unknown')
                        color_counts[color] = color_counts.get(color, 0) + 1
            except:
                pass
        
        result.update({
            'total_markers': total_markers,
            'files_with_markers': files_with_markers,
            'avg_per_file': round(total_markers / files_with_markers, 1) if files_with_markers else 0,
            'by_color': [{'color': k, 'count': v} 
                        for k, v in sorted(color_counts.items(), key=lambda x: x[1], reverse=True)]
        })
    
    return jsonify(result)


@app.route('/api/stats/<field_name>/wordfreq')
def field_wordfreq(field_name):
    """Get word frequency for a text-based field.
    
    Query params:
        limit=20        Number of top words to return (default 20)
        stopwords=1     Filter common stop words
        countonly=1     Just return the total unique word count
    """
    import re as re_module
    
    fields = db.get_fields()
    field = next((f for f in fields if f['name'].lower() == field_name.lower()), None)
    if not field:
        return jsonify({'error': 'Field "' + field_name + '" not found'}), 404
    
    limit = int(request.args.get('limit', 20))
    use_stopwords = request.args.get('stopwords', '0') == '1'
    count_only = request.args.get('countonly', '0') == '1'
    
    stop_words = set()
    if use_stopwords:
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'is', 'it', 'as', 'was', 'are', 'be',
            'this', 'that', 'these', 'those', 'has', 'have', 'had', 'not', 'no',
            'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might',
            'i', 'you', 'he', 'she', 'we', 'they', 'me', 'him', 'her', 'us', 'them',
            'my', 'your', 'his', 'its', 'our', 'their', 'what', 'which', 'who',
            'so', 'if', 'then', 'than', 'too', 'very', 'can', 'just', 'about',
            'up', 'out', 'all', 'been', 'when', 'where', 'how', 'each', 'there'
        }
    
    conn = sqlite3.connect(db.db_path)
    c = conn.cursor()
    c.execute('SELECT fv.value, fv.content FROM field_values fv WHERE fv.field_id = ? AND fv.value IS NOT NULL AND fv.value != ""', (field['id'],))
    rows = c.fetchall()
    conn.close()
    
    word_counts = {}
    
    for row in rows:
        texts = []
        
        if field['type'] == 'markers':
            try:
                markers = json.loads(row[0])
                texts = [m.get('label', '') for m in markers]
            except:
                pass
        elif field['type'] == 'tags':
            try:
                tags = json.loads(row[0])
                texts = tags
            except:
                pass
        elif field['type'] == 'datalink' and row[1]:
            texts = [row[1]]
        else:
            texts = [row[0]]
        
        for text in texts:
            if not text:
                continue
            words = re_module.findall(r'[a-zA-Z\u00C0-\u00FF\u0100-\u024F]+', text.lower())
            for word in words:
                if word and word not in stop_words:
                    word_counts[word] = word_counts.get(word, 0) + 1
    
    unique_count = len(word_counts)
    
    if count_only:
        return jsonify({
            'field': field['name'],
            'type': field['type'],
            'unique_words': unique_count
        })
    
    sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    
    return jsonify({
        'field': field['name'],
        'type': field['type'],
        'unique_words': unique_count,
        'showing': min(limit, len(sorted_words)),
        'words': [{'word': w, 'count': c} for w, c in sorted_words]
    })


@app.route('/api/query', methods=['POST'])
def query_files():
    """Query files based on conditions"""
    data = request.json
    conditions = data.get('conditions', [])
    sort_by = data.get('sort_by', '')
    sort_order = data.get('sort_order', 'asc')
    
    # Get all files
    files = db.get_files()
    
    # Filter by conditions
    if conditions:
        filtered_files = []
        for file in files:
            file_detail = db.get_file(file['id'])
            matches = True
            
            for condition in conditions:
                field_id = int(condition['field_id'])
                operator = condition['operator']
                query_value = condition['value']
                
                # Get field info
                field = next((f for f in db.get_fields() if f['id'] == field_id), None)
                if not field:
                    continue
                
                # Get file's value for this field
                file_value = file_detail['fields'].get(field['name'], '')
                
                # Evaluate condition
                condition_match = False
                if field['type'] == 'parameter':
                    try:
                        file_num = float(file_value) if file_value else 0
                        query_num = float(query_value)
                        if operator == '=':
                            condition_match = file_num == query_num
                        elif operator == '>':
                            condition_match = file_num > query_num
                        elif operator == '<':
                            condition_match = file_num < query_num
                    except ValueError:
                        condition_match = False
                elif field['type'] in ['choice', 'text']:
                    if operator == '=':
                        condition_match = str(file_value).lower() == query_value.lower()
                    elif operator == 'contains':
                        condition_match = query_value.lower() in str(file_value).lower()
                elif field['type'] == 'tags':
                    # Search within field-specific tags
                    try:
                        tags = json.loads(file_value) if file_value else []
                        if operator == 'contains':
                            condition_match = any(query_value.lower() in t.lower() for t in tags)
                        elif operator == '=':
                            condition_match = query_value.lower() in [t.lower() for t in tags]
                    except:
                        condition_match = False
                elif field['type'] == 'markers':
                    # Search within marker labels
                    try:
                        markers = json.loads(file_value) if file_value else []
                        if operator == 'contains':
                            condition_match = any(query_value.lower() in m.get('label', '').lower() for m in markers)
                    except:
                        condition_match = False
                elif field['type'] == 'datalink':
                    # Search both the path AND the indexed content
                    indexed_content = file_detail['fields'].get(field['name'] + '_content', '')
                    if operator == 'contains':
                        # Search in path OR in indexed content
                        condition_match = (query_value.lower() in str(file_value).lower() or 
                                          query_value.lower() in str(indexed_content).lower())
                    elif operator == '=':
                        condition_match = str(file_value).lower() == query_value.lower()
                
                if not condition_match:
                    matches = False
                    break
            
            if matches:
                filtered_files.append(file_detail)
        
        files = filtered_files
    else:
        # Get detailed info for all files
        files = [db.get_file(f['id']) for f in files]
    
    # Sort files
    if sort_by:
        field = next((f for f in db.get_fields() if f['id'] == int(sort_by)), None)
        if field:
            def sort_key(f):
                val = f['fields'].get(field['name'], '')
                if field['type'] == 'parameter':
                    try:
                        return float(val) if val else 0
                    except ValueError:
                        return 0
                return str(val).lower()
            
            files.sort(key=sort_key, reverse=(sort_order == 'desc'))
    else:
        # Sort by filename
        files.sort(key=lambda f: f['filename'].lower(), reverse=(sort_order == 'desc'))
    
    return jsonify({'files': files, 'count': len(files)})

# =====================================================================
# Periodic backup snapshots
# =====================================================================
# Copies the active database to a timestamped file inside a `backups/`
# directory next to the database, every `backup_interval_minutes` minutes.
# Default: 10 minutes. Adjust at runtime via POST /api/backup/interval.

backup_interval_minutes = 10.0
backup_thread_started = False
_backup_stop = threading.Event()

def _make_backup_snapshot():
    """Copy current_db_path to backups/<base>_backup_<timestamp>.db. Returns the path or None."""
    global current_db_path
    try:
        if not current_db_path or not os.path.exists(current_db_path):
            return None
        db_dir = os.path.dirname(os.path.abspath(current_db_path)) or '.'
        base = os.path.splitext(os.path.basename(current_db_path))[0]
        backups_dir = os.path.join(db_dir, 'backups')
        os.makedirs(backups_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        backup_path = os.path.join(backups_dir, f'{base}_backup_{timestamp}.db')
        shutil.copy2(current_db_path, backup_path)
        print(f"[backup] snapshot saved: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"[backup] error: {e}")
        return None

def _backup_loop():
    # Sleep first so we don't snapshot the moment the app launches.
    while not _backup_stop.is_set():
        # Sleep in small slices so interval changes take effect promptly
        # and shutdown is responsive.
        target = max(1.0, float(backup_interval_minutes)) * 60.0
        elapsed = 0.0
        while elapsed < target and not _backup_stop.is_set():
            time.sleep(min(5.0, target - elapsed))
            elapsed += 5.0
        if _backup_stop.is_set():
            break
        _make_backup_snapshot()

def _start_backup_thread():
    global backup_thread_started
    if backup_thread_started:
        return
    backup_thread_started = True
    t = threading.Thread(target=_backup_loop, daemon=True)
    t.start()
    print(f"[backup] thread started, interval = {backup_interval_minutes} min")

@app.route('/api/backup/snapshot', methods=['POST'])
def manual_backup_route():
    path = _make_backup_snapshot()
    if path:
        return jsonify({'success': True, 'path': path})
    return jsonify({'success': False, 'error': 'No active database'}), 400

@app.route('/api/backup/interval', methods=['GET', 'POST'])
def backup_interval_route():
    global backup_interval_minutes
    if request.method == 'POST':
        try:
            minutes = float((request.json or {}).get('minutes'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Invalid interval'}), 400
        if minutes <= 0:
            return jsonify({'success': False, 'error': 'Interval must be > 0'}), 400
        backup_interval_minutes = minutes
        return jsonify({'success': True, 'minutes': backup_interval_minutes})
    return jsonify({'minutes': backup_interval_minutes, 'running': backup_thread_started})


if __name__ == '__main__':
    import sys

    # Reopen the last-used database from the previous session, if it still exists.
    _last = _load_settings().get('last_db')
    if _last and os.path.exists(_last) and os.path.abspath(_last) != current_db_path:
        try:
            db = EarmarkDB(_last)
            current_db_path = os.path.abspath(_last)
            print(f"Reopened last database: {current_db_path}")
        except Exception as e:
            print(f"Could not reopen last database ({_last}): {e}")

    if '--script' in sys.argv:
        # Interactive Python mode
        print("Earmark Python Console")
        print("Database:", DB_PATH)
        print("\nAvailable commands:")
        print("  db.get_files()")
        print("  db.add_field('name', 'type')")
        print("  db.set_value(file_id, field_id, value)")
        print("  db.query(conditions)")
        print("\nType exit() to quit\n")
        
        import code
        code.interact(local=locals())
    else:
        # Web interface mode
        print("\nEarmark Audio Metadata Manager")
        print(f"Database: {DB_PATH}")

        # Start periodic backup snapshots (default every 10 minutes)
        _start_backup_thread()

        # Local port: keep 5000 if free, otherwise let the OS pick one
        # (5000 is often taken by macOS AirPlay Receiver)
        import socket
        def _pick_port(preferred=5000):
            for p in (preferred, 0):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.bind(('127.0.0.1', p))
                    return s.getsockname()[1]
                except OSError:
                    continue
                finally:
                    s.close()
        PORT = _pick_port()
        URL = 'http://127.0.0.1:%d' % PORT
        
        # Try pywebview for native window, fall back to browser
        try:
            import webview
            import threading
            
            def start_server():
                app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False, threaded=True)
            
            # Start Flask in background thread
            server = threading.Thread(target=start_server, daemon=True)
            server.start()
            
            # Give server a moment to start
            import time
            # Wait until Flask is accepting connections (max ~5 s)
            for _i in range(50):
                try:
                    socket.create_connection(('127.0.0.1', PORT), timeout=0.1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            
            print("Opening native window...")
            print("   (Close the window to quit)\n")
            
            # Create native window
            _ui_window = webview.create_window(
                'Earmark',
                URL,
                width=1400,
                height=900,
                min_size=(900, 600)
            )
            webview.start()
            
        except ImportError:
            # pywebview not installed, fall back to browser
            print("Opening in browser at " + URL)
            print("   (Install pywebview for native window: pip install pywebview)")
            print("\nPress Ctrl+C to stop\n")
            
            import webbrowser
            import threading
            threading.Timer(1.0, lambda: webbrowser.open(URL)).start()
            
            app.run(host='127.0.0.1', port=PORT, debug=True, threaded=True)

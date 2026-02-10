"""
Newsletter Generator for Aardvark Tactical and Project 7 Armor
Separate codebase from podcast-agent
"""

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import sqlite3
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import uuid
import httpx
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin, urlparse
from starlette.middleware.sessions import SessionMiddleware

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on system env vars

app = FastAPI(title="Newsletter Generator", version="1.0.0")

# Session middleware - use a secret key from environment or generate one
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    import secrets
    SESSION_SECRET_KEY = secrets.token_urlsafe(32)
    print("WARNING: SESSION_SECRET_KEY not set. Generated a random key.")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

# Authentication password
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin")

# Simple database configuration for Render
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "newsletters.db"))
print(f"[CONFIG] Database path: {DB_PATH}")

# Ensure database directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Upload and output directories
UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "outputs")

# Ensure directories exist
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

# Mount static files and templates
# On Vercel, static files should be served from the static directory
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception as e:
    print(f"Warning: Could not mount static files: {e}")

# For uploads, mount the directory (will use /tmp/uploads on Vercel)
try:
    app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
except Exception as e:
    print(f"Warning: Could not mount uploads directory: {e}")

templates = Jinja2Templates(directory="templates")

# =============================================================================
# AUTHENTICATION
# =============================================================================

# Simplified auth - just use plain password comparison
# No need for hashing since it's a single admin password

def check_auth(request: Request) -> bool:
    """Check if user is authenticated"""
    is_authenticated = "authenticated" in request.session and request.session.get("authenticated") == True
    print(f"[AUTH] check_auth called - session keys: {list(request.session.keys())}, authenticated: {is_authenticated}")
    return is_authenticated

async def get_current_user(request: Request):
    """Get current authenticated user - raises 401 for API routes"""
    if not check_auth(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return request.session.get("user")

# =============================================================================
# DATABASE SETUP
# =============================================================================

def get_db():
    """Get database connection with automatic database migration"""
    global _db_initialized
    
    # Check for database in old location and migrate if needed
    old_db_path = os.path.join(os.path.dirname(__file__), "newsletters.db")
    if os.path.exists(old_db_path) and old_db_path != DB_PATH:
        print(f"[GET_DB] Found database at old location: {old_db_path}")
        print(f"[GET_DB] Migrating to persistent location: {DB_PATH}")
        
        try:
            # Ensure new directory exists
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            
            # Copy old database to new location
            import shutil
            shutil.copy2(old_db_path, DB_PATH)
            print(f"[GET_DB] Database migrated successfully")
            
            # Verify migration worked
            if os.path.exists(DB_PATH):
                print(f"[GET_DB] Migration verified - new database size: {os.path.getsize(DB_PATH)} bytes")
                # Keep old database as backup but rename it
                backup_path = old_db_path + ".backup"
                shutil.move(old_db_path, backup_path)
                print(f"[GET_DB] Old database backed up to: {backup_path}")
            else:
                print("[GET_DB] Migration failed - database not found at new location")
        except Exception as e:
            print(f"[GET_DB] Database migration failed: {e}")
    
    # Check if database file already exists with data
    db_exists = os.path.exists(DB_PATH)
    has_data = False
    
    if db_exists:
        try:
            # Quick check if database has any newsletters (indicating real data)
            temp_conn = sqlite3.connect(DB_PATH)
            temp_cursor = temp_conn.cursor()
            temp_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='newsletters'")
            if temp_cursor.fetchone():
                temp_cursor.execute("SELECT COUNT(*) FROM newsletters")
                newsletter_count = temp_cursor.fetchone()[0]
                has_data = newsletter_count > 0
                print(f"[GET_DB] Existing database found with {newsletter_count} newsletters")
            temp_conn.close()
        except Exception as e:
            print(f"[GET_DB] Error checking existing database: {e}")
    
    # Only initialize if database doesn't exist or has no data
    if not _db_initialized and (not db_exists or not has_data):
        try:
            print("[GET_DB] Initializing new database...")
            init_db()
            _db_initialized = True
            print("[GET_DB] Database initialized successfully")
        except Exception as init_error:
            print(f"[GET_DB] Database initialization failed: {init_error}")
            raise
    elif has_data:
        _db_initialized = True
        print("[GET_DB] Using existing database with data")
    
    # Return SQLite connection
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
    
    # Try Turso first if configured
    if USE_DB_ADAPTER:
        print("[GET_DB] Using DB adapter (Turso)")
        try:
            print("[GET_DB] Calling get_db_adapter()...")
            adapter = get_db_adapter()
            print("[GET_DB] Adapter obtained successfully")
            return adapter
        except Exception as e:
            error_msg = f"Failed to connect to Turso database: {str(e)}"
            print(f"ERROR: {error_msg}")
            print("[GET_DB] Falling back to SQLite...")
            # Fall back to SQLite if Turso fails (unless on serverless)
            if IS_SERVERLESS or IS_VERCEL:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"{error_msg}\n\n"
                        "Please verify:\n"
                        "1. TURSO_DATABASE_URL is set correctly in environment variables\n"
                        "2. TURSO_AUTH_TOKEN is set correctly in environment variables\n"
                        "3. You have redeployed after adding the environment variables"
                    )
                )
            # For non-serverless, fall through to SQLite code below
            print("[GET_DB] Turso failed, falling through to SQLite")
    
    # Use SQLite (either by default or as fallback from Turso)
    print("[GET_DB] Using SQLite (not DB adapter)")
    # Safety check: Don't allow SQLite on Vercel (serverless)
    if IS_VERCEL:
        raise HTTPException(
            status_code=500,
            detail=(
                "Database configuration error: Running on Vercel (serverless) but Turso is not configured.\n\n"
                "REQUIRED: Set these environment variables:\n"
                "- TURSO_DATABASE_URL\n"
                "- TURSO_AUTH_TOKEN\n\n"
                "SQLite cannot be used on Vercel. On Render, SQLite works fine (persistent disk)."
            )
        )
    # Return SQLite connection with compatibility wrapper (local dev only)
    print(f"[GET_DB] Connecting to SQLite at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("[GET_DB] SQLite connection established")
    # Add compatibility methods
    class SQLiteWrapper:
            def __init__(self, conn):
                self.conn = conn
                self.use_turso = False
            def cursor(self):
                return self.conn.cursor()
            def commit(self):
                return self.conn.commit()
            def close(self):
                return self.conn.close()
            @property
            def lastrowid(self):
                # SQLite connections don't have lastrowid - it's on the cursor
                # This property exists for compatibility but should not be used
                # Use cursor.lastrowid instead after executing an INSERT
                raise AttributeError("Use cursor.lastrowid instead of conn.lastrowid for SQLite")
    return SQLiteWrapper(conn)

def init_db():
    """Initialize database tables and default brand configurations"""
    print(f"[INIT_DB] Initializing database at: {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Check if we already have data before doing anything
        try:
            cursor.execute("SELECT COUNT(*) FROM newsletters")
            existing_newsletters = cursor.fetchone()[0]
            if existing_newsletters > 0:
                print(f"[INIT_DB] Database already has {existing_newsletters} newsletters - skipping initialization")
                return
        except sqlite3.OperationalError:
            # Tables don't exist yet, that's fine
            print("[INIT_DB] Tables don't exist yet, creating them...")
        
        # Brands table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                config JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Newsletters table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS newsletters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                month TEXT NOT NULL,
                year INTEGER NOT NULL,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (brand_id) REFERENCES brands(id)
            )
        """)
        
        # Sections table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                newsletter_id INTEGER NOT NULL,
                section_type TEXT NOT NULL,
                section_order INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                content JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (newsletter_id) REFERENCES newsletters(id)
            )
        """)
        
        # Images table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                newsletter_id INTEGER,
                section_id INTEGER,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (newsletter_id) REFERENCES newsletters(id),
                FOREIGN KEY (section_id) REFERENCES sections(id)
            )
        """)
        
        # Eblasts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS eblasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                subject_line TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (brand_id) REFERENCES brands(id)
            )
        """)
        
        # Eblast sections table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS eblast_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                eblast_id INTEGER NOT NULL,
                section_type TEXT NOT NULL,
                section_order INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                content JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (eblast_id) REFERENCES eblasts(id)
            )
        """)
        
        # Insert default brand configurations ONLY if brands table is empty
        cursor.execute("SELECT COUNT(*) FROM brands")
        brand_count = cursor.fetchone()[0]
        
        if brand_count == 0:
            print("[INIT_DB] No brands found, inserting default brands...")
            brands = [
                ("project7", "PROJECT7 Armor", json.dumps(PROJECT7_CONFIG)),
                ("aardvark", "AARDVARK Tactical", json.dumps(AARDVARK_CONFIG))
            ]
            
            for name, display_name, config in brands:
                cursor.execute("""
                    INSERT INTO brands (name, display_name, config)
                    VALUES (?, ?, ?)
                """, (name, display_name, config))
                print(f"[INIT_DB] Inserted brand: {display_name}")
        else:
            print(f"[INIT_DB] Found {brand_count} existing brands, skipping brand insertion")
        
        conn.commit()
        print("[INIT_DB] Database initialization completed successfully")
    finally:
        conn.close()

# =============================================================================
# BRAND CONFIGURATIONS
# =============================================================================

PROJECT7_CONFIG = {
    "colors": {
        # Primary Palette
        "primary": "#565C43",         # OD Green (from accent palette, used for headers/footer)
        "primary_olive": "#757A4D",   # Olive Green (PMS 5763 C)
        "primary_black": "#2D2A26",   # Near Black (PMS BLACK C)
        # Secondary Palette  
        "accent": "#C0D330",           # Lime Green (PMS 382 C)
        "cool_grey": "#76777B",        # Cool Grey 9
        "black": "#000000",
        # Accent Palette (for backgrounds and details)
        "secondary_bg": "#CCCAC2",     # Light Tan/Beige
        "detail_bg": "#E6E7E8",        # Very Light Gray
        "warm_grey": "#9D9D8D",        # Warm Grey
        # Text colors
        "body_text": "#333333",
        "specs_text": "#666666",
        "footer_text": "#CCCAC2",
        "footer_muted": "#9D9D8D",
        "dark_accent": "#2D2A26",
        # Layout colors
        "border": "#CCCAC2"            # Subtle border for column dividers
    },
    "fonts": {
        "family": "Arial, Helvetica, sans-serif",  # Email-safe fallback
        "brand_fonts": ["United Sans Sm Cd", "Panton Narrow"],  # Official brand fonts
        "body_size": "16px",
        "header_size": "24px",
        "subheader_size": "18px",
        "small_size": "14px"
    },
    "logo_url": "https://p7img-20b64.kxcdn.com/web/image/website/2/logo/PROJECT7%20ARMOR?unique=4974767",
    "icon_url": "",  # P7 angular icon if needed
    "website_url": "https://www.project7armor.com",
    "contact_url": "https://www.project7armor.com/pages/contact-us-helpdesk",
    "newsletter_name": "Field Notes",
    "tagline": "PROJECT7 builds tactical equipment based on operator feedback. We solve specific problems, not everything.",
    "signature": "—The PROJECT7 Team"
}

AARDVARK_CONFIG = {
    "colors": {
        # Primary Palette
        "primary": "#03253E",          # DK Blue (official)
        "accent": "#F3E500",           # Yellow (official)
        # Secondary Palette
        "slate_blue": "#4B5E6F",       # Slate Blue
        "sky_blue": "#E3E8EE",         # Sky Blue
        "lt_sky_blue": "#EAEFF3",      # Lt Sky Blue
        "lt_beige": "#DBD8CF",         # Lt Beige
        # Functional mappings
        "secondary_bg": "#E3E8EE",     # Sky Blue - for alternating sections
        "detail_bg": "#EAEFF3",        # Lt Sky Blue - for detail sections
        "body_text": "#333333",
        "specs_text": "#4B5E6F",       # Slate Blue for specs
        "footer_text": "#E3E8EE",      # Sky Blue - light on dark footer
        "footer_muted": "#4B5E6F",     # Slate Blue
        "dark_accent": "#03253E",      # DK Blue for emphasis
        # Layout colors
        "border": "#DBD8CF"            # Lt Beige - subtle border for column dividers
    },
    "fonts": {
        "family": "Arial, Helvetica, sans-serif",  # Brand font is DIN, Arial as email-safe fallback
        "brand_font": "DIN",
        "body_size": "16px",
        "header_size": "24px",
        "subheader_size": "18px",
        "small_size": "14px"
    },
    # Logos - Delta A icon for header, full wordmark for footer
    "logo_url": "https://aardimg-20b64.kxcdn.com/web/image/website/3/logo/AARDVARK?unique=9f3b3b6",
    "icon_url": "https://aardimg-20b64.kxcdn.com/web/image/20063-43070711/02b_aard_logo_notagln_ko_nobckgrnd.png",  # White knockout Delta A
    "icon_dark_url": "",  # TODO: Upload 01a_icon_DeltaA_RGB.png to CDN and add URL here for dark blue Delta A
    "use_icon_header": True,  # Use Delta A icon in header instead of full logo
    "website_url": "https://www.aardvarktactical.com",
    "contact_url": "https://www.aardvarktactical.com/contactus",
    "newsletter_name": "AARD Report",
    "tagline": "AARDVARK finds, develops, and manufactures purpose-built products that enhance tactical operator safety.",
    "signature": "—The AARDVARK Team"
}

# Section definitions - what sections are available for NEWSLETTERS
SECTION_TYPES = [
    {"type": "header", "name": "Header", "description": "Logo and branding header", "required": True},
    {"type": "title", "name": "Title Bar", "description": "Newsletter name and date", "required": True},
    {"type": "opening", "name": "Opening Hook", "description": "Introduction and overview", "required": False},
    {"type": "feature", "name": "Featured Product", "description": "Main product spotlight", "required": False},
    {"type": "new_product", "name": "New Product", "description": "Secondary product highlight", "required": False},
    {"type": "details", "name": "Details Matter", "description": "Technical deep-dive", "required": False},
    {"type": "howto", "name": "How-To", "description": "Practical guidance section", "required": False},
    {"type": "event", "name": "See Us / Events", "description": "Event announcements", "required": False},
    {"type": "wrapup", "name": "Wrap Up", "description": "Closing and next month preview", "required": False},
    {"type": "footer", "name": "Footer", "description": "Links and unsubscribe", "required": True}
]

# Section definitions for EBLASTS (simpler, fewer options)
EBLAST_SECTION_TYPES = [
    {"type": "header", "name": "Header", "description": "Logo and branding header", "required": True},
    {"type": "hero", "name": "Hero Section", "description": "Main image and headline", "required": True},
    {"type": "body", "name": "Body Content", "description": "Main message and call-to-action", "required": True},
    {"type": "footer", "name": "Footer", "description": "Links and unsubscribe", "required": True}
]

# =============================================================================
# ROUTES - AUTHENTICATION
# =============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    print(f"[LOGIN] GET /login - session keys: {list(request.session.keys())}")
    
    # TEMPORARY: Auto-login for testing (remove password requirement)
    # Set SKIP_PASSWORD=1 in environment to enable
    skip_password = os.environ.get("SKIP_PASSWORD") == "1"
    print(f"[LOGIN] SKIP_PASSWORD={skip_password}")
    
    if skip_password:
        try:
            print("[LOGIN] Auto-login enabled, setting session...")
            request.session["authenticated"] = True
            request.session["user"] = "admin"
            print("[LOGIN] Redirecting to /")
            return RedirectResponse(url="/", status_code=303)
        except Exception as e:
            # If session fails, just show login page
            print(f"[LOGIN] Session error in login: {e}")
    
    # If already authenticated, redirect to home
    if request.session.get("authenticated"):
        print("[LOGIN] Already authenticated, redirecting to /")
        return RedirectResponse(url="/", status_code=303)
    
    # Render login page - this should work even if DB fails
    print("[LOGIN] Rendering login page")
    try:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": request.query_params.get("error")
        })
    except Exception as e:
        # Fallback if template rendering fails
        print(f"[LOGIN] Template error: {e}")
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><title>Login</title></head>
        <body style="font-family: Arial; padding: 40px; text-align: center;">
            <h1>Newsletter Generator</h1>
            <p>Login page is loading...</p>
            <p style="color: red;">Error: {str(e)}</p>
        </body>
        </html>
        """)

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    """Handle login form submission"""
    print(f"[LOGIN] POST /login - password provided: {bool(password)}")
    print(f"[LOGIN] APP_PASSWORD set: {bool(APP_PASSWORD)}")
    
    # Simple password check - in production, use hashed passwords
    if password == APP_PASSWORD:
        print("[LOGIN] Password correct, setting session...")
        try:
            request.session["authenticated"] = True
            request.session["user"] = "admin"
            print(f"[LOGIN] Session set - keys: {list(request.session.keys())}")
            print("[LOGIN] Redirecting to /")
            return RedirectResponse(url="/", status_code=303)
        except Exception as e:
            print(f"[LOGIN] Error setting session: {e}")
            return RedirectResponse(url="/login?error=Session+error", status_code=303)
    else:
        print("[LOGIN] Password incorrect")
        return RedirectResponse(url="/login?error=Invalid+password", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    """Logout user"""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

# =============================================================================
# ROUTES - PAGES
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - list all newsletters"""
    print(f"[HOME] GET / - session keys: {list(request.session.keys())}")
    
    # TEMPORARY: Auto-authenticate if SKIP_PASSWORD is set
    skip_password = os.environ.get("SKIP_PASSWORD") == "1"
    if skip_password:
        print("[HOME] SKIP_PASSWORD enabled, auto-authenticating...")
        try:
            request.session["authenticated"] = True
            request.session["user"] = "admin"
            print(f"[HOME] Session set - keys: {list(request.session.keys())}")
        except Exception as e:
            print(f"[HOME] Error setting session: {e}")
    
    # Check authentication for HTML routes
    is_auth = check_auth(request)
    print(f"[HOME] Authentication check: {is_auth}")
    if not is_auth:
        print("[HOME] Not authenticated, redirecting to /login")
        return RedirectResponse(url="/login", status_code=303)
    
    print("[HOME] Authenticated, loading home page...")
    
    # Try database, but fallback to empty state if it fails
    newsletters = []
    eblasts = []
    brands = []
    db_error = None
    
    try:
        print("[HOME] Attempting to get database connection...")
        conn = get_db()
        print("[HOME] Database connection obtained, creating cursor...")
        cursor = conn.cursor()
        print("[HOME] Executing query for newsletters...")
        
        # Get all newsletters with brand info
        cursor.execute("""
            SELECT n.*, b.display_name as brand_name, b.name as brand_slug
            FROM newsletters n
            JOIN brands b ON n.brand_id = b.id
            ORDER BY n.updated_at DESC
        """)
        print("[HOME] Fetching newsletter rows...")
        newsletters_rows = cursor.fetchall()
        newsletters = [dict(row) for row in newsletters_rows]
        print(f"[HOME] Found {len(newsletters)} newsletters")
        
        # Get all eblasts with brand info
        print("[HOME] Executing query for eblasts...")
        cursor.execute("""
            SELECT e.*, b.display_name as brand_name, b.name as brand_slug
            FROM eblasts e
            JOIN brands b ON e.brand_id = b.id
            ORDER BY e.updated_at DESC
        """)
        print("[HOME] Fetching eblast rows...")
        eblasts_rows = cursor.fetchall()
        eblasts = [dict(row) for row in eblasts_rows]
        print(f"[HOME] Found {len(eblasts)} eblasts")
        
        # Get brands for the create form
        print("[HOME] Executing query for brands...")
        cursor.execute("SELECT * FROM brands")
        brands_rows = cursor.fetchall()
        brands = [dict(row) for row in brands_rows]
        print(f"[HOME] Found {len(brands)} brands")
        
        conn.close()
        print("[HOME] Database queries completed successfully")
    except Exception as e:
        # Database failed - use default brands and empty newsletter list
        print(f"[HOME] Database error: {e}")
        import traceback
        print(f"[HOME] Traceback: {traceback.format_exc()}")
        db_error = str(e)
        print(f"Database error on home page: {e}")
        # Use default brands if DB fails
        brands = [
            {"id": 1, "name": "project7", "display_name": "PROJECT7 Armor"},
            {"id": 2, "name": "aardvark", "display_name": "AARDVARK Tactical"}
        ]
    
    return templates.TemplateResponse("home.html", {
        "request": request,
        "newsletters": newsletters,
        "eblasts": eblasts,
        "brands": brands,
        "db_error": db_error
    })

@app.get("/newsletter/{newsletter_id}", response_class=HTMLResponse)
async def edit_newsletter(request: Request, newsletter_id: int):
    """Newsletter editor page with tabbed sections"""
    # Check authentication for HTML routes
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    
    # Get newsletter
    cursor.execute("""
        SELECT n.*, b.display_name as brand_name, b.name as brand_slug, b.config as brand_config
        FROM newsletters n
        JOIN brands b ON n.brand_id = b.id
        WHERE n.id = ?
    """, (newsletter_id,))
    newsletter = cursor.fetchone()
    
    if not newsletter:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    
    # Get sections and convert to list of dicts
    cursor.execute("""
        SELECT * FROM sections
        WHERE newsletter_id = ?
        ORDER BY section_order
    """, (newsletter_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    # Get images and convert to list of dicts
    cursor.execute("""
        SELECT * FROM images
        WHERE newsletter_id = ?
    """, (newsletter_id,))
    images_rows = cursor.fetchall()
    images = [dict(row) for row in images_rows]
    
    conn.close()
    
    brand_config = json.loads(newsletter['brand_config'])
    
    return templates.TemplateResponse("editor.html", {
        "request": request,
        "newsletter": newsletter,
        "sections": sections,
        "images": images,
        "section_types": SECTION_TYPES,
        "brand_config": brand_config
    })

@app.get("/preview/{newsletter_id}", response_class=HTMLResponse)
async def preview_newsletter(request: Request, newsletter_id: int, version: str = "email"):
    """Preview generated newsletter HTML"""
    # Check authentication for HTML routes
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT n.*, b.config as brand_config
        FROM newsletters n
        JOIN brands b ON n.brand_id = b.id
        WHERE n.id = ?
    """, (newsletter_id,))
    newsletter_row = cursor.fetchone()
    
    if not newsletter_row:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    
    newsletter = dict(newsletter_row)
    
    cursor.execute("""
        SELECT * FROM sections
        WHERE newsletter_id = ? AND enabled = 1
        ORDER BY section_order
    """, (newsletter_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(newsletter['brand_config'])
    
    # Generate HTML based on version
    html = generate_newsletter_html(newsletter, sections, brand_config, version)
    
    return HTMLResponse(content=html)

# =============================================================================
# ROUTES - API
# =============================================================================

@app.post("/api/newsletters")
async def create_newsletter(
    request: Request,
    brand_id: int = Form(...),
    title: str = Form(...),
    month: str = Form(...),
    year: int = Form(...),
    user: dict = Depends(get_current_user)
):
    """Create a new newsletter with default sections"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create newsletter
        cursor.execute("""
            INSERT INTO newsletters (brand_id, title, month, year)
            VALUES (?, ?, ?, ?)
        """, (brand_id, title, month, year))
        
        # Get the last inserted row ID - in SQLite, this is on the cursor
        newsletter_id = cursor.lastrowid
        
        # Create default sections
        for i, section in enumerate(SECTION_TYPES):
            default_content = get_default_section_content(section['type'])
            cursor.execute("""
                INSERT INTO sections (newsletter_id, section_type, section_order, enabled, content)
                VALUES (?, ?, ?, ?, ?)
            """, (newsletter_id, section['type'], i, 1 if section['required'] else 0, json.dumps(default_content)))
        
        conn.commit()
        conn.close()
        
        return JSONResponse({"success": True, "newsletter_id": newsletter_id})
    except Exception as e:
        # Database failed - use file-based fallback
        print(f"Database error creating newsletter, using file fallback: {e}")
        # Generate a temporary ID
        newsletter_id = int(datetime.now().timestamp() * 1000)
        
        # Save to JSON file as fallback
        newsletter_data = {
            "id": newsletter_id,
            "brand_id": brand_id,
            "title": title,
            "month": month,
            "year": year,
            "status": "draft",
            "created_at": datetime.now().isoformat(),
            "sections": []
        }
        
        # Create sections
        for i, section in enumerate(SECTION_TYPES):
            default_content = get_default_section_content(section['type'])
            newsletter_data["sections"].append({
                "id": i + 1,
                "section_type": section['type'],
                "section_order": i,
                "enabled": 1 if section['required'] else 0,
                "content": default_content
            })
        
        # Save to file
        fallback_dir = os.path.join(OUTPUTS_DIR, "fallback")
        os.makedirs(fallback_dir, exist_ok=True)
        file_path = os.path.join(fallback_dir, f"newsletter_{newsletter_id}.json")
        with open(file_path, 'w') as f:
            json.dump(newsletter_data, f, indent=2)
        
        return JSONResponse({
            "success": True, 
            "newsletter_id": newsletter_id,
            "warning": "Saved to file (database unavailable)"
        })

@app.post("/newsletters/create")
async def create_newsletter_form(request: Request):
    """Handle newsletter creation from form and redirect to editor"""
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    
    form = await request.form()
    brand_id = int(form.get("brand_id"))
    title = form.get("title")
    month = form.get("month")
    year = int(form.get("year"))
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Create newsletter
        cursor.execute("""
            INSERT INTO newsletters (brand_id, title, month, year)
            VALUES (?, ?, ?, ?)
        """, (brand_id, title, month, year))
        
        newsletter_id = cursor.lastrowid
        
        # Create default sections for the newsletter
        for i, section in enumerate(SECTION_TYPES):
            default_content = get_default_section_content(section['type'])
            cursor.execute("""
                INSERT INTO sections (newsletter_id, section_type, section_order, enabled, content)
                VALUES (?, ?, ?, ?, ?)
            """, (newsletter_id, section['type'], i, 1 if section['required'] else 0, json.dumps(default_content)))
        
        conn.commit()
        conn.close()
        
        # Redirect to editor
        return RedirectResponse(url=f"/newsletter/{newsletter_id}", status_code=303)
        
    except Exception as e:
        print(f"Error creating newsletter: {e}")
        return RedirectResponse(url="/?error=creation_failed", status_code=303)

@app.post("/eblasts/create")
async def create_eblast_form(request: Request):
    """Handle eblast creation from form and redirect to editor"""
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    
    form = await request.form()
    brand_id = int(form.get("brand_id"))
    title = form.get("title")
    subject_line = form.get("subject_line", "")
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Create eblast
        cursor.execute("""
            INSERT INTO eblasts (brand_id, title, subject_line)
            VALUES (?, ?, ?)
        """, (brand_id, title, subject_line))
        
        eblast_id = cursor.lastrowid
        
        # Create default sections for the eblast
        for i, section in enumerate(EBLAST_SECTION_TYPES):
            default_content = get_default_eblast_section_content(section['type'])
            cursor.execute("""
                INSERT INTO eblast_sections (eblast_id, section_type, section_order, enabled, content)
                VALUES (?, ?, ?, ?, ?)
            """, (eblast_id, section['type'], i, 1 if section['required'] else 0, json.dumps(default_content)))
        
        conn.commit()
        conn.close()
        
        # Redirect to editor
        return RedirectResponse(url=f"/eblast/{eblast_id}", status_code=303)
        
    except Exception as e:
        print(f"Error creating eblast: {e}")
        return RedirectResponse(url="/?error=creation_failed", status_code=303)

@app.put("/api/sections/{section_id}")
async def update_section(section_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Update a section's content"""
    data = await request.json()
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE sections
        SET content = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (json.dumps(data.get('content', {})), data.get('enabled', 1), section_id))
    
    # Also update the newsletter's updated_at
    cursor.execute("""
        UPDATE newsletters
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = (SELECT newsletter_id FROM sections WHERE id = ?)
    """, (section_id,))
    
    conn.commit()
    conn.close()
    
    return JSONResponse({"success": True})

@app.post("/api/sections/{section_id}/toggle")
async def toggle_section(section_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Toggle a section on/off"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("UPDATE sections SET enabled = NOT enabled WHERE id = ?", (section_id,))
    cursor.execute("SELECT enabled FROM sections WHERE id = ?", (section_id,))
    result = cursor.fetchone()
    
    conn.commit()
    conn.close()
    
    return JSONResponse({"success": True, "enabled": bool(result['enabled'])})

@app.post("/api/images/upload")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    newsletter_id: int = Form(...),
    section_id: Optional[int] = Form(None),
    user: dict = Depends(get_current_user)
):
    """Upload an image"""
    # Generate unique filename
    ext = Path(file.filename).suffix
    unique_filename = f"{uuid.uuid4()}{ext}"
    filepath = os.path.join(UPLOADS_DIR, unique_filename)
    
    # Save file
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Save to database
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO images (newsletter_id, section_id, filename, original_filename, filepath)
        VALUES (?, ?, ?, ?, ?)
    """, (newsletter_id, section_id, unique_filename, file.filename, filepath))
    
    # Get the last inserted row ID - in SQLite, this is on the cursor
    image_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return JSONResponse({
        "success": True,
        "image_id": image_id,
        "filename": unique_filename,
        "url": f"/uploads/{unique_filename}"
    })

@app.post("/api/newsletters/{newsletter_id}/export")
async def export_newsletter(newsletter_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Export newsletter HTML files as downloads"""
    data = await request.json()
    version = data.get('version', 'both')  # email, website, or both
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT n.*, b.config as brand_config, b.name as brand_slug
        FROM newsletters n
        JOIN brands b ON n.brand_id = b.id
        WHERE n.id = ?
    """, (newsletter_id,))
    newsletter_row = cursor.fetchone()
    
    if not newsletter_row:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    
    newsletter = dict(newsletter_row)
    
    cursor.execute("""
        SELECT * FROM sections
        WHERE newsletter_id = ? AND enabled = 1
        ORDER BY section_order
    """, (newsletter_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(newsletter['brand_config'])
    
    # Create safe filename
    safe_title = "".join(c for c in newsletter['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
    base_filename = f"{newsletter['brand_slug']}_{newsletter['month']}_{newsletter['year']}_{safe_title}"
    
    # Handle single file download
    if version == 'email':
        html_content = generate_newsletter_html(newsletter, sections, brand_config, 'email')
        filename = f"{base_filename}_email.html"
        
        return Response(
            content=html_content,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
        )
    
    elif version == 'website':
        html_content = generate_newsletter_html(newsletter, sections, brand_config, 'website')
        filename = f"{base_filename}_website.html"
        
        return Response(
            content=html_content,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
        )
    
    else:  # version == 'both'
        # For 'both', return info that frontend needs to make two requests
        return JSONResponse({
            "success": True,
            "message": "both_versions_requested",
            "email_url": f"/api/newsletters/{newsletter_id}/export/email",
            "website_url": f"/api/newsletters/{newsletter_id}/export/website"
        })


@app.get("/api/newsletters/{newsletter_id}/export/email")
async def export_newsletter_email(newsletter_id: int, user: dict = Depends(get_current_user)):
    """Export newsletter email version as download"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT n.*, b.config as brand_config, b.name as brand_slug
        FROM newsletters n
        JOIN brands b ON n.brand_id = b.id
        WHERE n.id = ?
    """, (newsletter_id,))
    newsletter_row = cursor.fetchone()
    
    if not newsletter_row:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    
    newsletter = dict(newsletter_row)
    
    cursor.execute("""
        SELECT * FROM sections
        WHERE newsletter_id = ? AND enabled = 1
        ORDER BY section_order
    """, (newsletter_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(newsletter['brand_config'])
    
    html_content = generate_newsletter_html(newsletter, sections, brand_config, 'email')
    
    safe_title = "".join(c for c in newsletter['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
    filename = f"{newsletter['brand_slug']}_{newsletter['month']}_{newsletter['year']}_{safe_title}_email.html"
    
    return Response(
        content=html_content,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )


@app.get("/api/newsletters/{newsletter_id}/export/website")
async def export_newsletter_website(newsletter_id: int, user: dict = Depends(get_current_user)):
    """Export newsletter website version as download"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT n.*, b.config as brand_config, b.name as brand_slug
        FROM newsletters n
        JOIN brands b ON n.brand_id = b.id
        WHERE n.id = ?
    """, (newsletter_id,))
    newsletter_row = cursor.fetchone()
    
    if not newsletter_row:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    
    newsletter = dict(newsletter_row)
    
    cursor.execute("""
        SELECT * FROM sections
        WHERE newsletter_id = ? AND enabled = 1
        ORDER BY section_order
    """, (newsletter_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(newsletter['brand_config'])
    
    html_content = generate_newsletter_html(newsletter, sections, brand_config, 'website')
    
    safe_title = "".join(c for c in newsletter['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
    filename = f"{newsletter['brand_slug']}_{newsletter['month']}_{newsletter['year']}_{safe_title}_website.html"
    
    return Response(
        content=html_content,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )

@app.delete("/api/newsletters/{newsletter_id}")
async def delete_newsletter(newsletter_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Delete a newsletter and its sections"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM sections WHERE newsletter_id = ?", (newsletter_id,))
    cursor.execute("DELETE FROM images WHERE newsletter_id = ?", (newsletter_id,))
    cursor.execute("DELETE FROM newsletters WHERE id = ?", (newsletter_id,))
    
    conn.commit()
    conn.close()
    
    return JSONResponse({"success": True})

# =============================================================================
# ROUTES - EBLASTS API
# =============================================================================

@app.post("/api/eblasts")
async def create_eblast(
    request: Request,
    brand_id: int = Form(...),
    title: str = Form(...),
    subject_line: str = Form(""),
    user: dict = Depends(get_current_user)
):
    """Create a new eblast with default sections"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create eblast
        cursor.execute("""
            INSERT INTO eblasts (brand_id, title, subject_line)
            VALUES (?, ?, ?)
        """, (brand_id, title, subject_line))
        
        eblast_id = cursor.lastrowid
        
        # Create default sections for eblast
        for i, section in enumerate(EBLAST_SECTION_TYPES):
            default_content = get_default_eblast_section_content(section['type'])
            cursor.execute("""
                INSERT INTO eblast_sections (eblast_id, section_type, section_order, enabled, content)
                VALUES (?, ?, ?, ?, ?)
            """, (eblast_id, section['type'], i, 1, json.dumps(default_content)))
        
        conn.commit()
        conn.close()
        
        return JSONResponse({"success": True, "eblast_id": eblast_id})
    except Exception as e:
        print(f"Error creating eblast: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/eblast/{eblast_id}", response_class=HTMLResponse)
async def edit_eblast(request: Request, eblast_id: int):
    """Eblast editor page"""
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get eblast
    cursor.execute("""
        SELECT e.*, b.display_name as brand_name, b.name as brand_slug, b.config as brand_config
        FROM eblasts e
        JOIN brands b ON e.brand_id = b.id
        WHERE e.id = ?
    """, (eblast_id,))
    eblast = cursor.fetchone()
    
    if not eblast:
        raise HTTPException(status_code=404, detail="Eblast not found")
    
    # Get sections
    cursor.execute("""
        SELECT * FROM eblast_sections
        WHERE eblast_id = ?
        ORDER BY section_order
    """, (eblast_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(eblast['brand_config'])
    
    return templates.TemplateResponse("eblast_editor.html", {
        "request": request,
        "eblast": eblast,
        "sections": sections,
        "section_types": EBLAST_SECTION_TYPES,
        "brand_config": brand_config
    })


@app.put("/api/eblast_sections/{section_id}")
async def update_eblast_section(section_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Update an eblast section's content"""
    data = await request.json()
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE eblast_sections
        SET content = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (json.dumps(data.get('content', {})), data.get('enabled', 1), section_id))
    
    # Also update the eblast's updated_at
    cursor.execute("""
        UPDATE eblasts
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = (SELECT eblast_id FROM eblast_sections WHERE id = ?)
    """, (section_id,))
    
    conn.commit()
    conn.close()
    
    return JSONResponse({"success": True})


@app.get("/preview/eblast/{eblast_id}", response_class=HTMLResponse)
async def preview_eblast(request: Request, eblast_id: int):
    """Preview generated eblast HTML"""
    if not check_auth(request):
        return RedirectResponse(url="/login", status_code=303)
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT e.*, b.config as brand_config
        FROM eblasts e
        JOIN brands b ON e.brand_id = b.id
        WHERE e.id = ?
    """, (eblast_id,))
    eblast_row = cursor.fetchone()
    
    if not eblast_row:
        raise HTTPException(status_code=404, detail="Eblast not found")
    
    eblast = dict(eblast_row)
    
    cursor.execute("""
        SELECT * FROM eblast_sections
        WHERE eblast_id = ? AND enabled = 1
        ORDER BY section_order
    """, (eblast_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(eblast['brand_config'])
    
    html = generate_eblast_html(eblast, sections, brand_config)
    
    return HTMLResponse(content=html)


@app.post("/api/eblasts/{eblast_id}/export")
async def export_eblast(eblast_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Export eblast HTML file as download"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT e.*, b.config as brand_config, b.name as brand_slug
        FROM eblasts e
        JOIN brands b ON e.brand_id = b.id
        WHERE e.id = ?
    """, (eblast_id,))
    eblast_row = cursor.fetchone()
    
    if not eblast_row:
        raise HTTPException(status_code=404, detail="Eblast not found")
    
    eblast = dict(eblast_row)
    
    cursor.execute("""
        SELECT * FROM eblast_sections
        WHERE eblast_id = ? AND enabled = 1
        ORDER BY section_order
    """, (eblast_id,))
    sections_rows = cursor.fetchall()
    sections = [dict(row) for row in sections_rows]
    
    conn.close()
    
    brand_config = json.loads(eblast['brand_config'])
    
    # Generate HTML content
    html_content = generate_eblast_html(eblast, sections, brand_config)
    
    # Create safe filename
    safe_title = "".join(c for c in eblast['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
    filename = f"eblast_{eblast['brand_slug']}_{safe_title}.html"
    
    return Response(
        content=html_content,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )


@app.delete("/api/eblasts/{eblast_id}")
async def delete_eblast(eblast_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Delete an eblast and its sections"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM eblast_sections WHERE eblast_id = ?", (eblast_id,))
    cursor.execute("DELETE FROM eblasts WHERE id = ?", (eblast_id,))
    
    conn.commit()
    conn.close()
    
    return JSONResponse({"success": True})

@app.get("/api/brands/{brand_id}/config")
async def get_brand_config(brand_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Get brand configuration"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
    brand = cursor.fetchone()
    conn.close()
    
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    
    return JSONResponse(json.loads(brand['config']))

# =============================================================================
# AI CONTENT GENERATION 
# =============================================================================

# Get API key from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

async def scrape_product_page(url: str) -> dict:
    """Scrape product information from a URL"""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text
    except Exception as e:
        return {"error": f"Failed to fetch URL: {str(e)}"}
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove script and style elements
    for element in soup(['script', 'style', 'nav', 'footer', 'header']):
        element.decompose()
    
    # Extract title
    title = ""
    title_candidates = [
        soup.find('h1'),
        soup.find('meta', property='og:title'),
        soup.find('title')
    ]
    for candidate in title_candidates:
        if candidate:
            title = candidate.get('content', '') if candidate.name == 'meta' else candidate.get_text(strip=True)
            if title:
                break
    
    # Extract description/content
    description = ""
    
    # Try meta description first
    meta_desc = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', property='og:description')
    if meta_desc:
        description = meta_desc.get('content', '')
    
    # Get main content - look for product description areas
    content_selectors = [
        'div[class*="product-description"]',
        'div[class*="product-content"]',
        'div[class*="description"]',
        'div[class*="product-info"]',
        'article',
        'main',
        'div[class*="content"]'
    ]
    
    main_content = ""
    for selector in content_selectors:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(separator='\n', strip=True)
            if len(text) > len(main_content):
                main_content = text
    
    # Extract bullet points / features
    features = []
    for ul in soup.find_all('ul'):
        for li in ul.find_all('li'):
            text = li.get_text(strip=True)
            if text and len(text) > 10 and len(text) < 500:
                features.append(text)
    
    # Extract specifications from tables
    specs = []
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                value = cells[1].get_text(strip=True)
                if label and value:
                    specs.append(f"{label}: {value}")
    
    # Extract images
    images = []
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    
    # Look for product images
    img_selectors = [
        'img[class*="product"]',
        'img[class*="gallery"]',
        'div[class*="product"] img',
        'div[class*="gallery"] img',
        'img[src*="product"]',
        'picture img',
        'img'
    ]
    
    seen_srcs = set()
    for selector in img_selectors:
        for img in soup.select(selector)[:10]:  # Limit to 10 images
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if src:
                # Make absolute URL
                if src.startswith('//'):
                    src = 'https:' + src
                elif src.startswith('/'):
                    src = urljoin(base_url, src)
                elif not src.startswith('http'):
                    src = urljoin(url, src)
                
                # Filter out tiny images, icons, etc.
                if src not in seen_srcs and not any(skip in src.lower() for skip in ['icon', 'logo', 'pixel', '1x1', 'spacer', 'blank', 'placeholder']):
                    seen_srcs.add(src)
                    alt = img.get('alt', '')
                    images.append({"url": src, "alt": alt})
        
        if len(images) >= 5:  # Stop after finding enough images
            break
    
    return {
        "url": url,
        "title": title[:200] if title else "",
        "description": description[:1000] if description else "",
        "main_content": main_content[:5000] if main_content else "",
        "features": features[:15],
        "specs": specs[:20],
        "images": images[:8]
    }


async def generate_with_claude(prompt: str, system_prompt: str = "") -> str:
    """Call Claude API to generate content"""
    if not ANTHROPIC_API_KEY:
        return "[ERROR: ANTHROPIC_API_KEY not set. Set it as an environment variable.]"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
            )
            response.raise_for_status()
            result = response.json()
            return result['content'][0]['text']
    except httpx.HTTPStatusError as e:
        return f"[API Error: {e.response.status_code} - {e.response.text}]"
    except Exception as e:
        return f"[Error calling Claude API: {str(e)}]"


def get_brand_writing_system_prompt(brand_config: dict, section_type: str) -> str:
    """Get the system prompt for brand-appropriate writing"""
    brand_name = brand_config.get('newsletter_name', 'Newsletter')
    
    return f"""You are a skilled copywriter for tactical equipment newsletters. You write for {brand_name}.

Your audience is tactical operators, law enforcement, and military professionals. They are experienced, skeptical of marketing fluff, and want practical information.

Writing style guidelines:
- Be MEASURED over absolute. Use qualifiers like "may," "can," "typically" rather than absolutist claims
- Be CONCISE over comprehensive. Every sentence must earn its place
- Be ACTIONABLE over academic. Focus on what practitioners can do, not just what they should know
- Be RESPECTFUL of operational complexity. Never second-guess operators who face difficult situations
- NO em dashes (—). Use semicolons, colons, or commas instead
- NO marketing buzzwords or hype language
- Lead with the PROBLEM operators face, then the solution
- Include specific measurements and specs when available
- Write conversationally, like talking to a fellow operator
- Keep it professional but not stiff"""


def get_section_prompt(section_type: str, scraped_data: dict = None, guidance: str = "", supplemental_data: dict = None, input_content: str = "") -> str:
    """Get section-specific prompt for AI generation"""
    
    guidance_text = f"\n\nEDITOR'S GUIDANCE: {guidance}\nUse this to shape your writing." if guidance.strip() else ""
    
    # Build context from scraped data if available
    context = ""
    if scraped_data and 'error' not in scraped_data:
        context = f"""
SOURCE CONTENT (from {scraped_data.get('url', 'provided URL')}):

Title: {scraped_data.get('title', '')}
Description: {scraped_data.get('description', '')}

Main Content:
{scraped_data.get('main_content', '')[:2500]}

Features/Bullets:
{chr(10).join('- ' + f for f in scraped_data.get('features', [])[:10])}

Specs:
{chr(10).join('- ' + s for s in scraped_data.get('specs', [])[:10])}
"""
    
    if supplemental_data and 'error' not in supplemental_data:
        context += f"""

ADDITIONAL REFERENCE (from {supplemental_data.get('url', 'external source')}):
{supplemental_data.get('main_content', '')[:1500]}
Features: {chr(10).join('- ' + f for f in supplemental_data.get('features', [])[:8])}
"""
    
    if input_content and not scraped_data:
        context = f"\nINPUT CONTENT:\n{input_content}\n"
    
    # Section-specific prompts
    if section_type in ['feature', 'new_product']:
        return f"""{context}
{guidance_text}

Write content for a PRODUCT SECTION. Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "tagline": "Product Name: One compelling benefit statement",
    "problem": "2-3 sentences describing the challenge operators face. Make it relatable and specific.",
    "solution": "2-3 sentences explaining how this product addresses that problem.",
    "features": [
        {{"name": "Feature Name", "description": "Why this matters to operators"}},
        {{"name": "Feature Name", "description": "Why this matters to operators"}},
        {{"name": "Feature Name", "description": "Why this matters to operators"}}
    ],
    "why_it_matters": "A strong closing statement about operational impact.",
    "specs": "Brief technical specifications in one line."
}}

Remember: Lead with problems, be specific, no marketing fluff. Return ONLY the JSON object."""

    elif section_type == 'opening':
        return f"""{context}
{guidance_text}

Write content for the OPENING SECTION of the newsletter. This introduces what's in this issue and hooks the reader.

Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "hook": "A bold, attention-grabbing first line that starts with a problem or compelling statement. 1-2 sentences max.",
    "overview": "Brief preview of what's covered in this issue. 2-3 sentences that create anticipation without giving everything away."
}}

Make the hook punchy and problem-focused. The overview should tease value. Return ONLY the JSON object."""

    elif section_type == 'details':
        return f"""{context}
{guidance_text}

Write content for a "DETAILS MATTER" section. This is a technical deep-dive on a specific topic, feature, or design decision.

Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "title": "Short topic title (e.g., 'Radio Channel Routing', 'Thread Count', 'Buckle Design')",
    "subtitle": "A hook that explains why this detail matters (1 sentence)",
    "content": "The main explanation. 2-3 paragraphs explaining the technical detail, why it was designed this way, and what difference it makes operationally. Be specific with numbers and comparisons.",
    "closing": "A memorable closing line in italics style. Format: 'Small detail. Big difference when...' or similar."
}}

Focus on ONE specific detail. Make it educational but practical. Return ONLY the JSON object."""

    elif section_type == 'howto':
        return f"""{context}
{guidance_text}

Write content for a "HOW-TO" section. This provides practical, actionable guidance operators can use.

Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "title": "Action-oriented title (e.g., 'Properly Size Your Plate Carrier', 'Break In New Boots')",
    "intro": "1-2 sentences setting up why this matters and what they'll learn.",
    "subsections": [
        {{
            "heading": "Step or category heading",
            "items": ["Specific actionable item 1", "Specific actionable item 2", "Specific actionable item 3"]
        }},
        {{
            "heading": "Another step or category",
            "items": ["Item 1", "Item 2"]
        }}
    ],
    "key_principle": "A memorable takeaway principle or tip that ties it together."
}}

Keep items specific and actionable, not vague. Return ONLY the JSON object."""

    elif section_type == 'event':
        return f"""{context}
{guidance_text}

Write content for an EVENT ANNOUNCEMENT section.

Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "headline": "Section headline (e.g., 'See Us', 'On the Road', 'Meet the Team')",
    "event_name": "Name of the event or trade show",
    "dates": "Event dates (e.g., 'January 20-23, 2025')",
    "location": "City, State or venue name",
    "description": "1-2 sentences about what attendees can expect. Mention booth number if known, demos, new products to see.",
    "closing": "Call to action or invitation (e.g., 'Stop by booth 2847. First responders: coffee's on us.')"
}}

Keep it informative but inviting. Return ONLY the JSON object."""

    elif section_type == 'wrapup':
        return f"""{context}
{guidance_text}

Write content for the CLOSING/WRAP-UP section of the newsletter.

Return a JSON object with this exact structure (no markdown, just valid JSON):

{{
    "title": "Closing section title (e.g., 'What's Next', 'Coming Up', 'Until Next Time')",
    "next_month_preview": "1-2 sentences teasing what's coming in the next issue. Create anticipation.",
    "cta_text": "A friendly call-to-action inviting engagement (e.g., 'Questions about anything we covered? Hit reply. We read every message.')"
}}

Keep it brief and forward-looking. Return ONLY the JSON object."""

    else:
        # Generic fallback
        return f"""{context}
{guidance_text}

Write compelling newsletter content based on the above information.

Return a JSON object with relevant fields for the content. Return ONLY valid JSON."""


def get_structured_product_prompt(scraped_data: dict, guidance: str = "", supplemental_data: dict = None) -> str:
    """Build prompt that requests structured JSON output for product sections - wrapper for backwards compatibility"""
    return get_section_prompt('feature', scraped_data, guidance, supplemental_data)


@app.post("/api/ai/generate")
async def generate_content(request: Request, user: dict = Depends(get_current_user)):
    """Generate content using AI - section-aware"""
    data = await request.json()
    
    section_type = data.get('section_type', 'feature')
    prompt_type = data.get('prompt_type')  # 'from_url', 'from_text', 'polish_draft'
    input_content = data.get('input_content', '')
    guidance = data.get('guidance', '')
    supplemental_url = data.get('supplemental_url', '')
    brand_config = data.get('brand_config', {})
    
    system_prompt = get_brand_writing_system_prompt(brand_config, section_type)
    scraped_data = None
    supplemental_data = None
    images = []
    structured_content = None
    
    # Scrape URL if provided
    if prompt_type == 'from_url' and input_content.startswith('http'):
        scraped_data = await scrape_product_page(input_content)
        
        if 'error' in scraped_data:
            return JSONResponse({
                "success": False,
                "error": scraped_data['error']
            })
        
        images = scraped_data.get('images', [])
        
        # Scrape supplemental URL if provided
        if supplemental_url.strip():
            supplemental_data = await scrape_product_page(supplemental_url)
            if 'error' in supplemental_data:
                supplemental_data = None
    
    # Build section-specific prompt
    prompt = get_section_prompt(
        section_type=section_type,
        scraped_data=scraped_data,
        guidance=guidance,
        supplemental_data=supplemental_data,
        input_content=input_content if not scraped_data else ""
    )
    
    # Generate content with Claude
    generated_text = await generate_with_claude(prompt, system_prompt)
    
    # Try to parse as JSON
    try:
        cleaned = generated_text.strip()
        if cleaned.startswith('```'):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        
        structured_content = json.loads(cleaned)
    except json.JSONDecodeError:
        structured_content = None
    
    response_data = {
        "success": True,
        "section_type": section_type,
        "structured": structured_content is not None,
        "content": structured_content if structured_content else {"raw_text": generated_text},
        "images": images,
    }
    
    if scraped_data:
        response_data["scraped_data"] = {
            "title": scraped_data.get('title', ''),
            "url": scraped_data.get('url', ''),
            "image_count": len(images)
        }
    
    return JSONResponse(response_data)


@app.post("/api/scrape")
async def scrape_url(request: Request, user: dict = Depends(get_current_user)):
    """Just scrape a URL without generating content"""
    data = await request.json()
    url = data.get('url', '')
    
    if not url:
        return JSONResponse({"success": False, "error": "No URL provided"})
    
    scraped_data = await scrape_product_page(url)
    
    if 'error' in scraped_data:
        return JSONResponse({"success": False, "error": scraped_data['error']})
    
    return JSONResponse({
        "success": True,
        "data": scraped_data
    })

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_default_section_content(section_type: str) -> dict:
    """Get default content structure for a section type"""
    defaults = {
        "header": {"logo_url": ""},
        "title": {"newsletter_name": "", "month": "", "year": ""},
        "opening": {"hook": "", "overview": "", "image_url": "", "image_alt": ""},
        "feature": {
            "title": "",
            "tagline": "",
            "image_url": "",
            "image_alt": "",
            "problem": "",
            "solution": "",
            "features": [],
            "viewport_detail": "",
            "why_it_matters": "",
            "specs": "",
            "cta_count": 1,
            "ctas": [
                {"text": "", "url": ""}
            ],
            # Keep legacy fields for backward compatibility
            "cta_text": "",
            "cta_url": ""
        },
        "new_product": {
            "title": "",
            "tagline": "",
            "image_url": "",
            "image_alt": "",
            "problem": "",
            "solution": "",
            "features": [],
            "why_it_matters": "",
            "specs": "",
            "cta_count": 1,
            "ctas": [
                {"text": "", "url": ""}
            ],
            # Keep legacy fields for backward compatibility
            "cta_text": "",
            "cta_url": ""
        },
        "details": {
            "title": "",
            "subtitle": "",
            "image_url": "",
            "image_alt": "",
            "content": "",
            "closing": ""
        },
        "howto": {
            "title": "",
            "image_url": "",
            "image_alt": "",
            "intro": "",
            "subsections": [],
            "key_principle": ""
        },
        "event": {
            "headline": "",
            "image_url": "",
            "image_alt": "",
            "event_count": 1,
            "events": [
                {
                    "event_name": "",
                    "dates": "",
                    "location": "",
                    "description": ""
                }
            ],
            "closing": ""
        },
        "wrapup": {
            "title": "",
            "next_month_preview": "",
            "cta_text": "",
            "signature": "",
            "image_url": "",
            "image_alt": ""
        },
        "footer": {
            "tagline": "",
            "website_url": "",
            "contact_url": "",
            "preferences_url": "",
            "unsubscribe_url": ""
        }
    }
    return defaults.get(section_type, {})


def get_default_eblast_section_content(section_type: str) -> dict:
    """Get default content structure for an eblast section type"""
    defaults = {
        "header": {"logo_url": ""},
        "hero": {
            "headline": "",
            "subheadline": "",
            "image_url": "",
            "image_alt": ""
        },
        "body": {
            "image_url": "",
            "image_alt": "",
            "content": "",
            "cta_text": "",
            "cta_url": ""
        },
        "footer": {
            "tagline": "",
            "website_url": "",
            "contact_url": "",
            "preferences_url": "",
            "unsubscribe_url": ""
        }
    }
    return defaults.get(section_type, {})

def generate_newsletter_html(newsletter, sections, brand_config: dict, version: str) -> str:
    """Generate the complete newsletter HTML"""
    colors = brand_config['colors']
    fonts = brand_config['fonts']
    
    # Build sections HTML
    sections_html = []
    for section in sections:
        # Skip footer section for website version
        if version == "website" and section['section_type'] == 'footer':
            continue
            
        content = json.loads(section['content'])
        section_html = render_section(section['section_type'], content, brand_config, version)
        if section_html:
            sections_html.append(section_html)
    
    # Complete HTML template
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>{brand_config.get('newsletter_name', 'Newsletter')} - {newsletter['month']} {newsletter['year']}</title>
    <!--[if mso]>
    <style type="text/css">
        body, table, td {{font-family: {fonts['family']} !important;}}
    </style>
    <![endif]-->
</head>
<body style="margin: 0; padding: 0; background-color: #f4f4f4; font-family: {fonts['family']};">
    
    <!-- WRAPPER TABLE -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f4f4;">
        <tr>
            <td align="center" style="padding: 20px 0;">
                
                <!-- MAIN CONTAINER (600px) -->
                <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background-color: #ffffff; max-width: 600px;">
                    
                    {''.join(sections_html)}
                    
                </table>
                <!-- END MAIN CONTAINER -->
                
            </td>
        </tr>
    </table>
    <!-- END WRAPPER TABLE -->
    
</body>
</html>"""
    
    return html

def render_section(section_type: str, content: dict, brand_config: dict, version: str) -> str:
    """Render a single section to HTML"""
    colors = brand_config['colors']
    fonts = brand_config['fonts']
    
    # Helper to get background color with override support
    def get_bg_color(default_color: str) -> str:
        override = content.get('bg_color_override', '')
        if override and override.startswith('#'):
            return override
        return default_color
    
    # Helper to render optional image block
    def get_image_html(image_url: str = '', image_alt: str = '', padding: str = '0 0 20px 0') -> str:
        if not image_url:
            return ""
        return f"""
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="padding: {padding};">
                                        <img src="{image_url}" alt="{image_alt}" width="100%" style="display: block; width: 100%; height: auto; border-radius: 4px;">
                                    </td>
                                </tr>
                            </table>
        """
    
    if section_type == "header":
        # Use icon if use_icon_header is True, otherwise use full logo
        use_icon = brand_config.get('use_icon_header', False)
        if use_icon:
            # Use the icon (Delta A for Aardvark, angular logo for P7)
            logo_url = content.get('logo_url') or brand_config.get('icon_url') or brand_config.get('logo_url', '')
            logo_width = "120"  # Smaller width for icon
        else:
            logo_url = content.get('logo_url') or brand_config.get('logo_url', '')
            logo_width = "240"
        
        bg_color = get_bg_color(colors['primary'])
        return f"""
                    <!-- HEADER WITH LOGO -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 20px; text-align: center;">
                            <img src="{logo_url}" alt="{brand_config.get('newsletter_name', 'Newsletter')}" width="{logo_width}" style="display: block; margin: 0 auto; max-width: {logo_width}px; height: auto;">
                        </td>
                    </tr>
        """
    
    elif section_type == "title":
        newsletter_name = content.get('newsletter_name') or brand_config.get('newsletter_name', 'Newsletter')
        month = content.get('month', '')
        year = content.get('year', '')
        bg_color = get_bg_color(colors['accent'])
        return f"""
                    <!-- TITLE BAR -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 12px 20px; text-align: center;">
                            <p style="margin: 0; font-size: 14px; font-weight: bold; color: {colors['primary']}; text-transform: uppercase; letter-spacing: 1px;">
                                {newsletter_name} – {month} {year}
                            </p>
                        </td>
                    </tr>
        """
    
    elif section_type == "opening":
        hook = content.get('hook', '')
        overview = content.get('overview', '')
        if not hook and not overview:
            return ""
        bg_color = get_bg_color('#ffffff')
        image_html = get_image_html(content.get('image_url', ''), content.get('image_alt', ''), '20px 0 0 0')
        return f"""
                    <!-- OPENING HOOK -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px 20px 40px;">
                            <p style="margin: 0 0 20px 0; font-size: 18px; line-height: 1.6; color: {colors['body_text']}; font-weight: 600;">
                                {hook}
                            </p>
                            <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {overview}
                            </p>
                            {image_html}
                        </td>
                    </tr>
        """
    
    elif section_type == "feature":
        bg_color = get_bg_color('#ffffff')
        return render_product_section(content, brand_config, version, bg_color)
    
    elif section_type == "new_product":
        bg_color = get_bg_color(colors['secondary_bg'])
        return render_product_section(content, brand_config, version, bg_color)
    
    elif section_type == "details":
        title = content.get('title', '')
        subtitle = content.get('subtitle', '')
        body = content.get('content', '')
        closing = content.get('closing', '')
        if not title:
            return ""
        bg_color = get_bg_color(colors['detail_bg'])
        image_html = get_image_html(content.get('image_url', ''), content.get('image_alt', ''))
        return f"""
                    <!-- DETAILS MATTER -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px;">
                            <h2 style="margin: 0 0 15px 0; font-size: 20px; font-weight: bold; color: {colors['primary']}; text-transform: uppercase; text-align: center;">
                                Details Matter: {title}
                            </h2>
                            <p style="margin: 0 0 15px 0; font-size: 18px; line-height: 1.5; color: {colors['primary']}; font-weight: 600; text-align: center;">
                                {subtitle}
                            </p>
                            {image_html}
                            <p style="margin: 0 0 15px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {body}
                            </p>
                            <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']}; font-style: italic;">
                                {closing}
                            </p>
                        </td>
                    </tr>
        """
    
    elif section_type == "howto":
        title = content.get('title', '')
        intro = content.get('intro', '')
        subsections = content.get('subsections', [])
        key_principle = content.get('key_principle', '')
        if not title:
            return ""
        
        subsections_html = ""
        for sub in subsections:
            items_html = "".join([f"<li>{item}</li>" for item in sub.get('items', [])])
            subsections_html += f"""
                            <h3 style="margin: 20px 0 10px 0; font-size: 18px; font-weight: bold; color: {colors['primary']};">
                                {sub.get('heading', '')}
                            </h3>
                            <ul style="margin: 0 0 15px 20px; padding: 0; font-size: 16px; line-height: 1.8; color: {colors['body_text']};">
                                {items_html}
                            </ul>
            """
        
        bg_color = get_bg_color(colors['secondary_bg'])
        image_html = get_image_html(content.get('image_url', ''), content.get('image_alt', ''))
        return f"""
                    <!-- HOW-TO SECTION -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px;">
                            <h2 style="margin: 0 0 15px 0; font-size: 20px; font-weight: bold; color: {colors['primary']}; text-transform: uppercase;">
                                How-To: {title}
                            </h2>
                            {image_html}
                            <p style="margin: 0 0 15px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {intro}
                            </p>
                            {subsections_html}
                            <p style="margin: 20px 0 0 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']}; font-weight: 600; font-style: italic;">
                                {key_principle}
                            </p>
                        </td>
                    </tr>
        """
    
    elif section_type == "event":
        headline = content.get('headline', '')
        closing = content.get('closing', '')
        
        # Support both old format (single event) and new format (multiple events)
        events = content.get('events', [])
        
        # Backwards compatibility: if no events array, check for old single-event fields
        if not events:
            event_name = content.get('event_name', '')
            if event_name:
                events = [{
                    'event_name': event_name,
                    'dates': content.get('dates', ''),
                    'location': content.get('location', ''),
                    'description': content.get('description', '')
                }]
        
        # Filter out events with no event_name
        events = [e for e in events if e.get('event_name', '').strip()]
        
        if not events:
            return ""
        
        bg_color = get_bg_color(colors['accent'])
        
        # Build HTML for each event
        events_html = ""
        for i, event in enumerate(events):
            event_name = event.get('event_name', '')
            dates = event.get('dates', '')
            location = event.get('location', '')
            description = event.get('description', '')
            
            # Add separator between events (not before first or after last)
            separator = ""
            if i > 0:
                separator = f"""
                            <hr style="border: none; border-top: 1px solid {colors['primary']}; margin: 25px 40px; opacity: 0.3;">
                """
            
            events_html += f"""
                            {separator}
                            <p style="margin: 0 0 10px 0; font-size: 18px; line-height: 1.5; color: {colors['dark_accent']}; text-align: center; font-weight: 600;">
                                {event_name}
                            </p>
                            <p style="margin: 0 0 5px 0; font-size: 16px; line-height: 1.6; color: {colors['primary']}; text-align: center;">
                                {dates}
                            </p>
                            <p style="margin: 0 0 10px 0; font-size: 16px; line-height: 1.6; color: {colors['primary']}; text-align: center;">
                                {location}
                            </p>
                            <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['primary']}; text-align: center;">
                                {description}
                            </p>
            """
        
        # Closing message (shared across all events)
        closing_html = ""
        if closing:
            closing_html = f"""
                            <p style="margin: 25px 0 0 0; font-size: 16px; line-height: 1.6; color: {colors['dark_accent']}; text-align: center; font-style: italic; font-weight: 600;">
                                {closing}
                            </p>
            """
        
        return f"""
                    <!-- EVENT ANNOUNCEMENT -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px;">
                            <h2 style="margin: 0 0 15px 0; font-size: 24px; font-weight: bold; color: {colors['primary']}; text-transform: uppercase; text-align: center;">
                                {headline}
                            </h2>
                            {get_image_html(content.get('image_url', ''), content.get('image_alt', ''))}
                            {events_html}
                            {closing_html}
                        </td>
                    </tr>
        """
    
    elif section_type == "wrapup":
        title = content.get('title', '')
        preview = content.get('next_month_preview', '')
        cta = content.get('cta_text', 'Questions about anything we covered? Hit reply. We read every message.')
        signature = content.get('signature') or brand_config.get('signature', '')
        bg_color = get_bg_color('#ffffff')
        image_html = get_image_html(content.get('image_url', ''), content.get('image_alt', ''))
        return f"""
                    <!-- CLOSING SECTION -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: {bg_color};">
                            <h2 style="margin: 0 0 15px 0; font-size: 20px; font-weight: bold; color: {colors['primary']};">
                                {title}
                            </h2>
                            {image_html}
                            <p style="margin: 0 0 15px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {preview}
                            </p>
                            <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {cta}
                            </p>
                            <p style="margin: 20px 0 0 0; font-size: 16px; line-height: 1.6; color: {colors['primary']}; font-weight: 600;">
                                {signature}
                            </p>
                        </td>
                    </tr>
        """
    
    elif section_type == "footer":
        tagline = content.get('tagline') or brand_config.get('tagline', '')
        website = content.get('website_url') or brand_config.get('website_url', '#')
        contact = content.get('contact_url') or brand_config.get('contact_url', '#')
        prefs = content.get('preferences_url', 'YOUR_PREFERENCES_URL')
        unsub = content.get('unsubscribe_url', 'YOUR_UNSUBSCRIBE_URL')
        bg_color = get_bg_color(colors['primary'])
        return f"""
                    <!-- FOOTER -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px; text-align: center;">
                            <p style="margin: 0 0 15px 0; font-size: 14px; line-height: 1.6; color: {colors['footer_text']}; font-style: italic;">
                                {tagline}
                            </p>
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td align="center" style="padding: 10px 0;">
                                        <a href="{website}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Website</a>
                                        <span style="color: {colors['footer_muted']}; margin: 0 5px;">|</span>
                                        <a href="{contact}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Contact</a>
                                        <span style="color: {colors['footer_muted']}; margin: 0 5px;">|</span>
                                        <a href="{prefs}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Update Preferences</a>
                                    </td>
                                </tr>
                            </table>
                            <p style="margin: 20px 0 0 0; font-size: 12px; color: {colors['footer_muted']};">
                                <a href="{unsub}" style="color: {colors['footer_muted']}; text-decoration: underline;">Unsubscribe</a>
                            </p>
                        </td>
                    </tr>
        """
    
    return ""

def render_product_section(content: dict, brand_config: dict, version: str, bg_color: str) -> str:
    """Render a product section (feature or new_product)"""
    colors = brand_config['colors']
    
    title = content.get('title', '')
    tagline = content.get('tagline', '')
    if not tagline and not title:
        return ""
    
    image_url = content.get('image_url', '')
    image_alt = content.get('image_alt', '')
    problem = content.get('problem', '')
    solution = content.get('solution', '')
    features = content.get('features', [])
    viewport = content.get('viewport_detail', '')
    why = content.get('why_it_matters', '')
    specs = content.get('specs', '')
    
    # Handle both new multi-CTA format and legacy single CTA format
    cta_count = content.get('cta_count', 1)
    ctas = content.get('ctas', [])
    
    # Migration: If ctas is empty but legacy fields exist, convert them
    if not ctas and (content.get('cta_text') or content.get('cta_url')):
        ctas = [{
            'text': content.get('cta_text', 'Learn More'),
            'url': content.get('cta_url', '#')
        }]
        cta_count = 1
    
    # Ensure we have at least one CTA if count is set
    if cta_count > 0 and not ctas:
        ctas = [{'text': 'Learn More', 'url': '#'}]
    
    # Build features HTML
    features_html = ""
    for feat in features:
        name = feat.get('name', '')
        desc = feat.get('description', '')
        features_html += f"""
                                        <p style="margin: 0 0 8px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                            <strong style="color: {colors['primary']};">{name}</strong> – {desc}
                                        </p>
        """
    
    # Image HTML
    image_html = ""
    if image_url:
        image_html = f"""
                            <!-- FULL WIDTH IMAGE -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="padding: 0 0 20px 0;">
                                        <img src="{image_url}" alt="{image_alt}" width="100%" style="display: block; width: 100%; height: auto; border-radius: 4px;">
                                    </td>
                                </tr>
                            </table>
        """
    
    # Viewport detail (optional)
    viewport_html = ""
    if viewport:
        viewport_html = f"""
                            <p style="margin: 20px 0 15px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {viewport}
                            </p>
        """
    
    # Show full content in both email and website versions - no truncation
    truncate_note = ""
    
    # Title HTML (optional - only shown if title is provided)
    title_html = ""
    if title:
        title_html = f"""
                            <h2 style="margin: 0 0 10px 0; font-size: 24px; font-weight: bold; color: {colors['primary']}; text-transform: uppercase;">
                                {title}
                            </h2>
        """
    
    # Build CTA buttons HTML
    cta_buttons_html = ""
    if ctas:
        if len(ctas) == 1:
            # Single CTA - centered
            cta = ctas[0]
            cta_buttons_html = f"""
                            <!-- CTA BUTTON -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td align="center" style="padding-top: 25px;">
                                        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                                            <tr>
                                                <td style="background-color: {colors['accent']}; border-radius: 4px;">
                                                    <a href="{cta.get('url', '#')}" style="display: inline-block; padding: 14px 28px; font-size: 16px; font-weight: bold; color: {colors['primary']}; text-decoration: none; text-transform: uppercase; letter-spacing: 0.5px;">
                                                        {cta.get('text', 'Learn More')}
                                                    </a>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
            """
        else:
            # Multiple CTAs - side by side
            cta_cells = ""
            cta_spacing = "10px" if len(ctas) == 2 else "5px"
            for i, cta in enumerate(ctas):
                spacing_style = ""
                if i > 0:
                    spacing_style = f"padding-left: {cta_spacing};"
                if i < len(ctas) - 1:
                    spacing_style += f" padding-right: {cta_spacing};"
                
                cta_cells += f"""
                                                <td style="{spacing_style}">
                                                    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                                                        <tr>
                                                            <td style="background-color: {colors['accent']}; border-radius: 4px;">
                                                                <a href="{cta.get('url', '#')}" style="display: inline-block; padding: 12px 20px; font-size: 14px; font-weight: bold; color: {colors['primary']}; text-decoration: none; text-transform: uppercase; letter-spacing: 0.5px;">
                                                                    {cta.get('text', 'Learn More')}
                                                                </a>
                                                            </td>
                                                        </tr>
                                                    </table>
                                                </td>
                """
            
            cta_buttons_html = f"""
                            <!-- MULTIPLE CTA BUTTONS -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td align="center" style="padding-top: 25px;">
                                        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                                            <tr>
                                                {cta_cells}
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
            """
    
    return f"""
                    <!-- PRODUCT SECTION -->
                    <tr>
                        <td style="padding: 20px 40px; background-color: {bg_color};">
                            {title_html}
                            <p style="margin: 0 0 20px 0; font-size: 18px; line-height: 1.5; color: {colors['primary']}; font-weight: 600;">
                                {tagline}
                            </p>
                            
                            {image_html}
                            
                            <!-- TWO COLUMN TEXT - Balanced Layout -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="width: 50%; vertical-align: top; padding-right: 15px; border-right: 1px solid {colors.get('border', '#e0e0e0')};">
                                        <p style="margin: 0 0 12px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                            {problem}
                                        </p>
                                        <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                            {solution}
                                        </p>
                                    </td>
                                    <td style="width: 50%; vertical-align: top; padding-left: 15px;">
                                        {features_html}
                                    </td>
                                </tr>
                            </table>
                            
                            {viewport_html}
                            
                            <p style="margin: 20px 0 15px 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']}; font-weight: 600;">
                                Why it matters: {why}
                            </p>
                            
                            <p style="margin: 0 0 15px 0; font-size: 14px; line-height: 1.6; color: {colors["specs_text"]}; font-style: italic;">{specs}</p>
                            
                            {cta_buttons_html}
                        </td>
                    </tr>
    """

# =============================================================================
# EBLAST HTML GENERATION
# =============================================================================

def generate_eblast_html(eblast, sections, brand_config: dict) -> str:
    """Generate the complete eblast HTML"""
    colors = brand_config['colors']
    fonts = brand_config['fonts']
    
    # Build sections HTML
    sections_html = []
    for section in sections:
        content = json.loads(section['content'])
        section_html = render_eblast_section(section['section_type'], content, brand_config)
        if section_html:
            sections_html.append(section_html)
    
    # Complete HTML template
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>{eblast['title']}</title>
    <!--[if mso]>
    <style type="text/css">
        body, table, td {{font-family: {fonts['family']} !important;}}
    </style>
    <![endif]-->
</head>
<body style="margin: 0; padding: 0; background-color: #f4f4f4; font-family: {fonts['family']};">
    
    <!-- WRAPPER TABLE -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f4f4;">
        <tr>
            <td align="center" style="padding: 20px 0;">
                
                <!-- MAIN CONTAINER (600px) -->
                <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background-color: #ffffff; max-width: 600px;">
                    
                    {''.join(sections_html)}
                    
                </table>
                <!-- END MAIN CONTAINER -->
                
            </td>
        </tr>
    </table>
    <!-- END WRAPPER TABLE -->
    
</body>
</html>"""
    
    return html


def render_eblast_section(section_type: str, content: dict, brand_config: dict) -> str:
    """Render a single eblast section to HTML"""
    colors = brand_config['colors']
    fonts = brand_config['fonts']
    
    if section_type == "header":
        # Use icon if use_icon_header is True, otherwise use full logo
        use_icon = brand_config.get('use_icon_header', False)
        if use_icon:
            logo_url = content.get('logo_url') or brand_config.get('icon_url') or brand_config.get('logo_url', '')
            logo_width = "120"
        else:
            logo_url = content.get('logo_url') or brand_config.get('logo_url', '')
            logo_width = "240"
        
        bg_color = content.get('bg_color_override') or colors['primary']
        return f"""
                    <!-- HEADER WITH LOGO -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 20px; text-align: center;">
                            <img src="{logo_url}" alt="{brand_config.get('newsletter_name', 'Brand')}" width="{logo_width}" style="display: block; margin: 0 auto; max-width: {logo_width}px; height: auto;">
                        </td>
                    </tr>
        """
    
    elif section_type == "hero":
        headline = content.get('headline', '')
        subheadline = content.get('subheadline', '')
        image_url = content.get('image_url', '')
        image_alt = content.get('image_alt', '')
        bg_color = content.get('bg_color_override') or '#ffffff'
        
        image_html = ""
        if image_url:
            image_html = f"""
                            <img src="{image_url}" alt="{image_alt}" width="100%" style="display: block; width: 100%; height: auto;">
            """
        
        return f"""
                    <!-- HERO SECTION -->
                    <tr>
                        <td style="background-color: {bg_color};">
                            {image_html}
                            <div style="padding: 30px 40px;">
                                <h1 style="margin: 0 0 15px 0; font-size: 28px; line-height: 1.3; color: {colors['primary']}; font-weight: bold;">
                                    {headline}
                                </h1>
                                <p style="margin: 0; font-size: 18px; line-height: 1.5; color: {colors['body_text']};">
                                    {subheadline}
                                </p>
                            </div>
                        </td>
                    </tr>
        """
    
    elif section_type == "body":
        body_content = content.get('content', '')
        cta_text = content.get('cta_text', '')
        cta_url = content.get('cta_url', '#')
        image_url = content.get('image_url', '')
        image_alt = content.get('image_alt', '')
        bg_color = content.get('bg_color_override') or '#ffffff'
        
        # Convert newlines to <br> for HTML
        body_content_html = body_content.replace('\n', '<br>')
        
        image_html = ""
        if image_url:
            image_html = f"""
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td style="padding: 0 0 20px 0;">
                                        <img src="{image_url}" alt="{image_alt}" width="100%" style="display: block; width: 100%; height: auto; border-radius: 4px;">
                                    </td>
                                </tr>
                            </table>
            """
        
        cta_html = ""
        if cta_text:
            cta_html = f"""
                            <!-- CTA BUTTON -->
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td align="center" style="padding-top: 25px;">
                                        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                                            <tr>
                                                <td style="background-color: {colors['accent']}; border-radius: 4px;">
                                                    <a href="{cta_url}" style="display: inline-block; padding: 14px 28px; font-size: 16px; font-weight: bold; color: {colors['primary']}; text-decoration: none; text-transform: uppercase; letter-spacing: 0.5px;">
                                                        {cta_text}
                                                    </a>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
            """
        
        return f"""
                    <!-- BODY CONTENT -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 20px 40px 30px 40px;">
                            {image_html}
                            <p style="margin: 0; font-size: 16px; line-height: 1.6; color: {colors['body_text']};">
                                {body_content_html}
                            </p>
                            {cta_html}
                        </td>
                    </tr>
        """
    
    elif section_type == "footer":
        tagline = content.get('tagline') or brand_config.get('tagline', '')
        website = content.get('website_url') or brand_config.get('website_url', '#')
        contact = content.get('contact_url') or brand_config.get('contact_url', '#')
        prefs = content.get('preferences_url', 'YOUR_PREFERENCES_URL')
        unsub = content.get('unsubscribe_url', 'YOUR_UNSUBSCRIBE_URL')
        bg_color = content.get('bg_color_override') or colors['primary']
        
        return f"""
                    <!-- FOOTER -->
                    <tr>
                        <td style="background-color: {bg_color}; padding: 30px 40px; text-align: center;">
                            <p style="margin: 0 0 15px 0; font-size: 14px; line-height: 1.6; color: {colors['footer_text']}; font-style: italic;">
                                {tagline}
                            </p>
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
                                <tr>
                                    <td align="center" style="padding: 10px 0;">
                                        <a href="{website}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Website</a>
                                        <span style="color: {colors['footer_muted']}; margin: 0 5px;">|</span>
                                        <a href="{contact}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Contact</a>
                                        <span style="color: {colors['footer_muted']}; margin: 0 5px;">|</span>
                                        <a href="{prefs}" style="color: {colors['accent']}; text-decoration: none; font-size: 14px; margin: 0 10px;">Update Preferences</a>
                                    </td>
                                </tr>
                            </table>
                            <p style="margin: 20px 0 0 0; font-size: 12px; color: {colors['footer_muted']};">
                                <a href="{unsub}" style="color: {colors['footer_muted']}; text-decoration: underline;">Unsubscribe</a>
                            </p>
                        </td>
                    </tr>
        """
    
    return ""

# =============================================================================
# STARTUP
# =============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    print("Newsletter Generator starting...")
    print(f"Database: {DB_PATH}")
    print(f"Password: {'Set' if APP_PASSWORD != 'admin' else 'Using default (admin)'}")

# Lazy database initialization - only initialize when first needed
_db_initialized = False

@app.middleware("http")
async def ensure_db_middleware(request: Request, call_next):
    """Ensure database is initialized - only check once per app instance"""
    print(f"[MIDDLEWARE] {request.method} {request.url.path}")
    # Skip database initialization in middleware - let routes handle it
    # This prevents hanging on first request
    try:
        response = await call_next(request)
        print(f"[MIDDLEWARE] Response status: {response.status_code}")
        return response
    except Exception as e:
        print(f"[MIDDLEWARE] Error: {e}")
        raise

@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "ok", "service": "newsletter-generator"}

@app.get("/debug/db")
async def debug_database():
    """Simple database diagnostic endpoint"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Count records in main tables
        cursor.execute("SELECT COUNT(*) as count FROM brands")
        brands_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM newsletters")
        newsletters_count = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM sections")
        sections_count = cursor.fetchone()['count']
        
        # Get latest newsletters
        cursor.execute("SELECT id, title, month, year, updated_at FROM newsletters ORDER BY updated_at DESC LIMIT 3")
        recent_newsletters = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return {
            "status": "ok",
            "database": {
                "path": DB_PATH,
                "exists": os.path.exists(DB_PATH),
                "size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
                "table_counts": {
                    "brands": brands_count,
                    "newsletters": newsletters_count,
                    "sections": sections_count
                },
                "recent_newsletters": recent_newsletters
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/debug/backup")
async def backup_database():
    """Create a backup of the database"""
    try:
        if not os.path.exists(DB_PATH):
            return {"status": "error", "error": "Database not found"}
        
        # Create backup with timestamp
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{DB_PATH}.backup_{timestamp}"
        
        # Copy database file
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        
        return {
            "status": "ok",
            "backup_path": backup_path,
            "backup_size": os.path.getsize(backup_path),
            "timestamp": timestamp
        }
    except Exception as e:
        return {
            "status": "error", 
            "error": str(e)
        }

@app.get("/favicon.ico")
async def favicon():
    """Return empty favicon to stop 404 errors"""
    from fastapi.responses import Response
    return Response(content=b"", media_type="image/x-icon")

@app.get("/simple")
async def simple_page():
    """Ultra-simple page with no dependencies - use this to test if app is working"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>Simple Test</title></head>
    <body style="font-family: Arial; padding: 40px;">
        <h1>✅ Simple Page Works!</h1>
        <p>No database, no sessions, no dependencies.</p>
        <p><a href="/test">Go to Test Page</a></p>
        <p><a href="/login">Go to Login Page</a></p>
        <p><a href="/health">Check Health</a></p>
    </body>
    </html>
    """)

@app.get("/test")
async def test_page():
    """Simple test page to verify app is working"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>Test</title></head>
    <body style="font-family: Arial; padding: 40px;">
        <h1>✅ App is Working!</h1>
        <p>If you can see this, the FastAPI app is running correctly.</p>
        <p><a href="/login">Go to Login Page</a></p>
        <p><a href="/simple">Go to Simple Page</a></p>
    </body>
    </html>
    """)

@app.get("/test")
async def test_page():
    """Simple test page to verify app is working"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>Test</title></head>
    <body style="font-family: Arial; padding: 40px;">
        <h1>✅ App is Working!</h1>
        <p>If you can see this, the FastAPI app is running correctly.</p>
        <p><a href="/login">Go to Login Page</a></p>
        <p><a href="/?skip_auth=1">Go to Home (Skip Auth)</a></p>
    </body>
    </html>
    """)

@app.get("/simple")
async def simple_page():
    """Ultra-simple page with no dependencies"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>Simple Test</title></head>
    <body style="font-family: Arial; padding: 40px;">
        <h1>Simple Page Works!</h1>
        <p>No database, no sessions, no dependencies.</p>
    </body>
    </html>
    """)

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT environment variable, fallback to 8001 for local dev
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)

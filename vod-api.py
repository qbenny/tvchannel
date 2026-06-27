import os
import json
import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import threading
import time
from contextlib import asynccontextmanager

from run_simulator import STBDeviceConfig, STBSimulator, load_stb_config, parse_epg_json

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup actions
    load_poster_cache()
    login_sim()
    start_heartbeat_thread()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/settings")

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc: Exception):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse(url="/settings")
    return JSONResponse(status_code=404, content={"detail": "Not Found"})

# ==========================================
# 1. IPTV STB Simulator Initialization & Auth
# ==========================================

config_data = load_stb_config()
config_stb = STBDeviceConfig(
    user_id=config_data.get("user_id"),
    stb_id=config_data.get("stb_id"),
    mac_address=config_data.get("mac_address"),
    ip_address=config_data.get("ip_address"),
    base_url=config_data.get("base_url"),
    des_key=config_data.get("des_key")
)
sim = STBSimulator(config=config_stb)

def login_sim() -> bool:
    try:
        print(">>> [STB Simulator] Logging in via STBSimulator.login()...")
        success = sim.login()
        if success:
            print(f">>> [STB Simulator] Login successful. EPG Gateway: {sim.state.epg_base_url}")
            return True
        else:
            print(">>> [STB Simulator] STBSimulator.login() returned False.")
            sim.state.is_authenticated = False
            return False
    except Exception as e:
        print(f">>> [STB Simulator] Login failed with exception: {e}")
        sim.state.is_authenticated = False
        return False

def ensure_authenticated():
    if not sim.state.is_authenticated:
        login_sim()

def start_heartbeat_thread():
    sim.state.heartbeat_interval = 60  # Keep session alive every 60 seconds
    
    def run_heartbeat():
        print(">>> [Heartbeat Thread] Started.")
        while True:
            try:
                if sim.state.is_authenticated:
                    sim.keep_alive()
            except Exception as e:
                print(f">>> [Heartbeat Thread] Error: {e}")
            time.sleep(5)
            
    thread = threading.Thread(target=run_heartbeat, daemon=True)
    thread.start()

# ==========================================
# 2. Local Fallback Database Parsing
# ==========================================

poster_cache = {}
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

POSTER_CACHE_FILE = os.path.join(DATA_DIR, "poster_cache.json")
VOD_CATEGORIES_FILE = os.path.join(DATA_DIR, "vod_categories.json")
VOD_SOURCE_TREE_CACHE_FILE = os.path.join(DATA_DIR, "vod_source_tree_cache.json")
VIS_DOMAIN = "http://115.233.200.59:58007/epg/" #待修改

# Shared state for background source-tree refresh
source_tree_refresh_status = {
    "refreshing": False,
    "last_updated": None,
    "error": None,
    "done": 0,
    "total": 0
}

def load_vod_categories():
    def clean_categories(cats):
        if not isinstance(cats, list):
            return cats
        for cat in cats:
            filters = cat.get("filters", [])
            cleaned_filters = []
            for filt in filters:
                if filt.get("key") == "sub_type":
                    cleaned_filters.append(filt)
                    break
            cat["filters"] = cleaned_filters
        return cats

    if os.path.exists(VOD_CATEGORIES_FILE):
        try:
            with open(VOD_CATEGORIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    # Old list format, migrate to dict format
                    migrated_categories = data
                    if len(data) == 2:
                        ids = {c.get("id") for c in data}
                        if ids == {"movies", "series"}:
                            default_path = os.path.join(DATA_DIR, "vod_categories_default.json")
                            if os.path.exists(default_path):
                                try:
                                    with open(default_path, "r", encoding="utf-8") as f_def:
                                        def_data = json.load(f_def)
                                        if isinstance(def_data, dict) and "categories" in def_data:
                                            migrated_categories = def_data["categories"]
                                except Exception as e_def:
                                    print(f"Error reading default categories template: {e_def}")
                    
                    migrated_categories = clean_categories(migrated_categories)
                    new_data = {
                        "version": 2,
                        "categories": migrated_categories
                    }
                    try:
                        with open(VOD_CATEGORIES_FILE, "w", encoding="utf-8") as f2:
                            json.dump(new_data, f2, ensure_ascii=False, indent=2)
                    except Exception as ex:
                        print(f"Error saving migrated categories: {ex}")
                    return migrated_categories
                elif isinstance(data, dict) and "categories" in data:
                    return clean_categories(data["categories"])
        except Exception as e:
            print(f"Error loading vod_categories.json: {e}")
            
    # Fresh start: load from default template, save as version 2 dict, and return
    categories = []
    default_path = os.path.join(DATA_DIR, "vod_categories_default.json")
    if os.path.exists(default_path):
        try:
            with open(default_path, "r", encoding="utf-8") as f_def:
                def_data = json.load(f_def)
                if isinstance(def_data, dict) and "categories" in def_data:
                    categories = def_data["categories"]
        except Exception as e_def:
            print(f"Error reading default categories template: {e_def}")
            
    if not categories:
        categories = [
            {
                "id": "movies",
                "name": "电影专区",
                "filters": [
                    {
                        "key": "sub_type",
                        "name": "分类",
                        "value": []
                    }
                ]
            },
            {
                "id": "series",
                "name": "电视剧场",
                "filters": [
                    {
                        "key": "sub_type",
                        "name": "分类",
                        "value": []
                    }
                ]
            }
        ]
        
    categories = clean_categories(categories)
    try:
        save_data = {
            "version": 2,
            "categories": categories
        }
        with open(VOD_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving default categories: {e}")
        
    return categories


poster_lock = threading.Lock()

def load_poster_cache():
    global poster_cache
    if os.path.exists(POSTER_CACHE_FILE):
        try:
            with poster_lock:
                with open(POSTER_CACHE_FILE, "r", encoding="utf-8") as f:
                    poster_cache = json.load(f)
            print(f">>> [Poster Cache] Loaded {len(poster_cache)} items from disk.")
        except Exception as e:
            print(f">>> [Poster Cache] Error loading poster cache: {e}")
            poster_cache = {}
    else:
        poster_cache = {}

def save_poster_cache():
    try:
        with poster_lock:
            with open(POSTER_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(poster_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f">>> [Poster Cache] Error saving poster cache: {e}")

# ==========================================
# 2b. VOD Source Tree: Live Fetch & Cache
# ==========================================

def _fetch_leaf_counts(tree):
    """For every leaf-level category node, hit VIS API with size=1 to get recordCount."""
    global source_tree_refresh_status

    leaf_nodes = []
    for root in tree:
        for child in root["children"]:
            if not child["children"]:
                leaf_nodes.append(child)
            else:
                leaf_nodes.extend(child["children"])

    source_tree_refresh_status["total"] = len(leaf_nodes)
    source_tree_refresh_status["done"]  = 0

    from concurrent.futures import ThreadPoolExecutor
    status_lock = threading.Lock()

    def fetch_count(node):
        global source_tree_refresh_status
        try:
            url = f"{VIS_DOMAIN}api/categoryitem/{node['id']}.json"
            res = sim.state.session.get(
                url,
                params={"pageindex": "1", "size": "1", "userId": sim.config.user_id},
                headers=sim.config.headers,
                timeout=10
            )
            if res.status_code == 200:
                data = res.json()
                node["count"] = int(data.get("pageInfo", {}).get("recordCount", 0) or 0)
        except Exception as e:
            node["count"] = 0
            print(f">>> [Source Tree] Count fetch failed for {node['id']}: {e}")
        finally:
            with status_lock:
                source_tree_refresh_status["done"] += 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        executor.map(fetch_count, leaf_nodes)

    return tree


def refresh_source_tree_bg():
    """Background task: parse tree structure + fetch live counts + persist cache."""
    global source_tree_refresh_status
    source_tree_refresh_status["refreshing"] = True
    source_tree_refresh_status["error"]      = None

    try:
        ensure_authenticated()
        if os.path.exists(VOD_SOURCE_TREE_CACHE_FILE):
            with open(VOD_SOURCE_TREE_CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                tree = cache_data.get("tree", [])
        else:
            raise Exception("No cached source tree structure found to refresh.")

        tree = _fetch_leaf_counts(tree)

        import time as _time
        cache_data = {"tree": tree, "updated_at": _time.time()}
        with open(VOD_SOURCE_TREE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

        source_tree_refresh_status["last_updated"] = cache_data["updated_at"]
        print(f">>> [Source Tree] Refresh done. {source_tree_refresh_status['done']} categories enriched.")
    except Exception as e:
        source_tree_refresh_status["error"] = str(e)
        print(f">>> [Source Tree] Refresh failed: {e}")
    finally:
        source_tree_refresh_status["refreshing"] = False


# ==========================================
# 3. TVBox Config & API Handlers
# ==========================================

@app.get("/config") #待修改
async def get_tvbox_config(request: Request):
    api_url = str(request.base_url) + "api/vod"
    
    cfg_ver = "1"
    if os.path.exists(VOD_CATEGORIES_FILE):
        cfg_ver = str(int(os.path.getmtime(VOD_CATEGORIES_FILE)))
        
    config_data = {
        "sites": [
            {
                "key": f"Telecom_VOD_{cfg_ver}",
                "name": "浙江电信点播",
                "type": 1,
                "api": api_url,
                "playUrl": "json:" + str(request.base_url) + "api/play?vod_id=",
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 1
            }
        ]
    }
    return JSONResponse(content=config_data)

@app.get("/api/vod")
async def handle_tvbox_request(request: Request):
    ac = request.query_params.get("ac", "")
    t = request.query_params.get("t", "")
    pg = request.query_params.get("pg", "1")
    wd = request.query_params.get("wd", "")
    ids = request.query_params.get("ids", "")

    print(f">>> [API Request] ac={ac}, t={t}, pg={pg}, wd={wd}, ids={ids}, query_params={dict(request.query_params)}")

    page = int(pg) if pg.isdigit() else 1

    # ------------------------------------------
    # 场景 1: 获取视频详情
    # ------------------------------------------
    if ac == "detail" and ids:
        id_list = ids.split(",")
        detail_list = []
        for current_id in id_list:
            # 说明是通过实时分类/实时搜索拉取到的视频 ID，格式为 {item_type}_{item_code}
            parts = current_id.split("_", 1)
            if len(parts) != 2:
                continue
            item_type, item_code = parts
            
            try:
                ensure_authenticated()
                data_url = f"{sim.state.epg_base_url}/EPG/jsp/gdhdpublic/Ver.2/common/data.jsp"
                
                # A. 根据电信 itemCode 变换出内部物理 vod_id
                params_code = {
                    "Action": "vodIdByCode",
                    "foreignSN": item_code,
                    "contentType": "0"
                }
                res_code = sim.state.session.get(data_url, params=params_code, headers=sim.config.headers, timeout=10)
                data_code = parse_epg_json(res_code.text)
                vod_id = data_code.get("result", {}).get("id")
                if not vod_id:
                    continue
                vod_id = str(vod_id)
                
                # B. 单个电影资源：使用延迟加载链接，避免一次性生成导致的高频认证失败
                if item_type == "vod":
                    result_vod = sim.get_vod_info(vod_id) or {}
                    
                    play_url = vod_id
                    name = result_vod.get("name") or f"{item_code} (电影)"
                    content = result_vod.get("introduce") or "热播大片专区"
                    
                    with poster_lock:
                        pic_url = poster_cache.get(current_id) or (str(request.base_url) + "pics/default_poster.jpg")
                    detail_list.append({
                        "vod_id": current_id,
                        "vod_name": name,
                        "vod_pic": pic_url,
                        "type_name": "电影",
                        "vod_content": content,
                        "vod_play_from": "电信专线",
                        "vod_play_url": f"播放${play_url}",
                        "vod_remarks": "高清"
                    })
                    
                # C. 电视剧资源：遍历分集列表，统一封装为代理接口
                elif item_type == "series":
                    series_info = sim.get_series_info(vod_id)
                    if not series_info:
                        continue
                    
                    name = series_info.get("name") or f"{item_code} (电视剧)"
                    content = series_info.get("introduce") or "热播剧集专区"
                    episode_list = series_info.get("episodes", [])
                    
                    ep_play_urls = []
                    for idx, ep in enumerate(episode_list):
                        ep_id = ep.get("id")
                        num_str = ep.get("num")
                        if num_str and num_str.isdigit():
                            ep_num = int(num_str)
                        else:
                            ep_num = idx + 1
                        
                        # Handle placeholder / missing episodes (EPG returns "缺" or non-digit IDs)
                        if not ep_id or ep_id == "缺" or "缺" in ep_id or not ep_id.isdigit():
                            ep_play_urls.append(f"第{ep_num}集(缺)$")
                        else:
                            play_url = ep_id
                            ep_play_urls.append(f"第{ep_num}集${play_url}")
                        
                    with poster_lock:
                        pic_url = poster_cache.get(current_id) or (str(request.base_url) + "pics/default_poster.jpg")
                    detail_list.append({
                        "vod_id": current_id,
                        "vod_name": name,
                        "vod_pic": pic_url,
                        "type_name": "电视剧",
                        "vod_content": content,
                        "vod_play_from": "电信专线",
                        "vod_play_url": "#".join(ep_play_urls),
                        "vod_remarks": f"更新至{len(ep_play_urls)}集" if ep_play_urls else "暂无内容"
                    })
            except Exception as e:
                print(f"动态详情查询失败 for {current_id}: {e}")
                    
        return JSONResponse(content={"code": 1, "list": detail_list})

    # ------------------------------------------
    # 场景 2: 获取分类列表 / 实时搜索
    # ------------------------------------------
    elif (ac == "list" or ac == "detail") and (t or wd):
        # 实时搜索分支 (EPG Action=search)
        if wd:
            vod_list = []
            try:
                ensure_authenticated()
                data_url = f"{sim.state.epg_base_url}/EPG/jsp/gdhdpublic/Ver.2/common/data.jsp"
                params_search = {
                    "Action": "search",
                    "keyword": wd,
                    "posterflag": "1",
                    "displayflag": "0",
                    "columnStart": "0",
                    "columnLength": "40",
                    "vodLength": "40"
                }
                res = sim.state.session.get(data_url, params=params_search, headers=sim.config.headers, timeout=10)
                if res.status_code == 200:
                    data = parse_epg_json(res.text)
                    result_set = data.get("result", [])
                    if isinstance(result_set, list):
                        for item in result_set:
                            name = item.get("name", "Unknown")
                            telecom_code = item.get("telecomCode", "")
                            item_type_val = item.get("type", "0")
                            item_type = "series" if item_type_val == "1" else "vod"
                            
                            vod_list.append({
                                "vod_id": f"{item_type}_{telecom_code}",
                                "vod_name": name,
                                "vod_pic": str(request.base_url) + "pics/default_poster.jpg",
                                "vod_remarks": "电影" if item_type == "vod" else "电视剧"
                            })
            except Exception as e:
                print(f"Live search failed: {e}")
                
            return JSONResponse(content={
                "code": 1,
                "page": page,
                "pagecount": (len(vod_list) // 20) + 1,
                "limit": 20,
                "total": len(vod_list),
                "list": vod_list
            })
            
        # 分类列表分支 (VIS api/categoryitem)
        if t:
            sub_type = request.query_params.get("sub_type", "")
            
            # TVBox standard: check if filters are passed in the 'f' query parameter
            f_param = request.query_params.get("f", "")
            if f_param:
                try:
                    import base64
                    try:
                        padded = f_param + "=" * ((4 - len(f_param) % 4) % 4)
                        decoded_str = base64.b64decode(padded).decode('utf-8')
                        f_json = json.loads(decoded_str)
                    except Exception:
                        # Try raw JSON
                        f_json = json.loads(f_param)
                        
                    if isinstance(f_json, dict):
                        if "sub_type" in f_json:
                            sub_type = f_json["sub_type"]
                        print(f">>>> [API] Parsed filters from 'f': {f_json}")
                except Exception as e:
                    print(f">>> [API] Failed to parse filter parameter 'f' ({f_param}): {e}")

            if sub_type:
                target_cat = sub_type
            else:
                # Resolve target category dynamically from user's custom categories first
                target_cat = None
                custom_cats = load_vod_categories()
                for cat in custom_cats:
                    if cat.get("id") == t:
                        filters = cat.get("filters", [])
                        for filt in filters:
                            if filt.get("key") == "sub_type":
                                vals = filt.get("value", [])
                                for val in vals:
                                    if val.get("v"):
                                        target_cat = val.get("v")
                                        break
                                if not target_cat and vals:
                                    target_cat = vals[0].get("v")
                                break
                        break
                
                if not target_cat:
                    target_cat = t
                    
            vod_list = []
            try:
                vis_domain = "http://115.233.200.59:58007/epg/"
                
                # Dynamic mapping from user setting - no hardcoded routing tables needed
                query_cat = target_cat
                
                url_items = f"{vis_domain}api/categoryitem/{query_cat}.json"
                params_items = {
                    "pageindex": str(page),
                    "size": "20",
                    "userId": sim.config.user_id
                }
                res = requests.get(url_items, params=params_items, headers=sim.config.headers, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    status = data.get("status")
                    result_set = data.get("resultSet")
                    
                    if status == 200 and isinstance(result_set, list) and (result_set or page > 1):
                        image_server = data.get("imageServer", "http://115.233.200.61:58001/pics")
                        
                        for item in result_set:
                            item_type = item.get("itemType", "vod")
                            # Skip non-playable assets (like links or subjects) which fail to play in TVBox
                            if item_type not in ["vod", "series"]:
                                continue
                                
                            title = item.get("title", "Unknown")
                            item_code = item.get("itemCode", "")
                            icon = item.get("itemIcon") or item.get("contentPictures", {}).get("poster1") or ""
                            pic_url = f"{image_server}{icon}" if icon and not icon.startswith("http") else icon
                            if not pic_url:
                                pic_url = str(request.base_url) + "pics/default_poster.jpg"
                            
                            item_id = f"{item_type}_{item_code}"
                            with poster_lock:
                                poster_cache[item_id] = pic_url
                            
                            vod_list.append({
                                "vod_id": item_id,
                                "vod_name": title,
                                "vod_pic": pic_url,
                                "vod_remarks": "电影" if item_type == "vod" else "电视剧"
                             })
                        save_poster_cache()
                        
                        page_info = data.get("pageInfo", {})
                        total_count = page_info.get("recordCount", len(vod_list))
                        page_count = page_info.get("pageCount", page)
                            
                        return JSONResponse(content={
                            "code": 1,
                            "page": page,
                            "pagecount": page_count,
                            "limit": 20,
                            "total": total_count,
                            "list": vod_list
                        })
            except Exception as e:
                print(f"Live category items fetch failed for {target_cat} (routed to {query_cat if 'query_cat' in locals() else target_cat}): {e}")

            return JSONResponse(content={"code": 1, "page": page, "pagecount": 1, "limit": 20, "total": 0, "list": []})

    # ------------------------------------------
    # 场景 3: 初始化请求，返回顶级分类
    # ------------------------------------------
    else:
        custom_cats = load_vod_categories()
        
        vis_categories = []
        vis_filters = {}
        
        for cat in custom_cats:
            cat_id = cat.get("id")
            cat_name = cat.get("name")
            cat_filters = cat.get("filters", [])
            
            vis_categories.append({"type_id": cat_id, "type_name": cat_name})
            vis_filters[cat_id] = cat_filters
                
        return JSONResponse(content={"code": 1, "class": vis_categories, "filters": vis_filters})

# ==========================================
# 4. Lazy Playback Redirection Resolver
# ==========================================

@app.get("/api/play")
@app.get("/api/play.ts")
@app.get("/api/play/{vod_id_path}.ts")
async def play_redirect(request: Request, vod_id: str = None, url: str = None, vod_id_path: str = None):
    if vod_id_path:
        vod_id = vod_id_path
        
    # Extract original parameters if wrapped by TVBox playUrl prefixing
    if vod_id and "api/play" in vod_id:
        if "url=" in vod_id:
            url = vod_id.split("url=", 1)[1]
            vod_id = None
        elif "vod_id=" in vod_id:
            vod_id = vod_id.split("vod_id=", 1)[1]

    # Clean empty strings to None
    if url == "":
        url = None
    if vod_id == "":
        vod_id = None

    if not url and not vod_id:
        return JSONResponse(content={"error": "Missing vod_id or url parameter"}, status_code=400)

    # Passthrough direct playback URLs
    target_url = None
    if url:
        target_url = url
    elif vod_id and (vod_id.startswith("http://") or vod_id.startswith("https://") or vod_id.startswith("rtsp://")):
        target_url = vod_id
        
    if target_url:
        if target_url.startswith("rtsp://"):
            target_url = target_url.replace("rtsp://", "http://")
        print(f">>> [Resolver] Passthrough play URL (rewritten to HTTP): {target_url}")
        return JSONResponse(content={
            "parse": 0,
            "url": target_url,
            "header": {
                "User-Agent": "CTC-2k/1.0 EPG/3.0 STB"
            }
        })
        
    # Otherwise, resolve the dynamic EPG vod_id
    if vod_id:
        try:
            ensure_authenticated()
            media_url = sim.get_vod_play_url(vod_id)
            if media_url:
                target_url = media_url.split("?")[0]
        except Exception as e:
            print(f">>> [Resolver] Error resolving play URL for vod_id {vod_id}: {e}")
            
    if target_url:
        if target_url.startswith("rtsp://"):
            target_url = target_url.replace("rtsp://", "http://")
        print(f">>> [Resolver] Resolved EPG play URL (rewritten to HTTP): {target_url}")
        return JSONResponse(content={
            "parse": 0,
            "url": target_url,
            "header": {
                "User-Agent": sim.config.headers.get("User-Agent", "CTC-2k/1.0 EPG/3.0 STB")
            }
        })
        
    return JSONResponse(content={"error": "Play URL resolution failed"}, status_code=404)

@app.get("/pics/default_poster.jpg")
async def get_default_poster():
    file_path = "default_poster.png"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="image/png")
    return Response(status_code=404)

# ==========================================
# 4. Web Dashboard API Endpoints
# ==========================================

@app.get("/settings")
async def get_settings():
    return RedirectResponse(url="/static/index.html")

@app.get("/api/stb-config")
async def get_stb_config():
    return load_stb_config()

@app.post("/api/stb-config")
async def save_stb_config(config_in: dict):
    try:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        config_path = os.path.join(data_dir, "stb_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_in, f, ensure_ascii=False, indent=2)
        
        # Reinitialize active simulator configuration
        global config_stb, sim
        config_stb = STBDeviceConfig(
            user_id=config_in.get("user_id"),
            stb_id=config_in.get("stb_id"),
            mac_address=config_in.get("mac_address"),
            ip_address=config_in.get("ip_address"),
            base_url=config_in.get("base_url"),
            des_key=config_in.get("des_key")
        )
        sim.config = config_stb
        sim.state.is_authenticated = False # Force re-auth
        
        return {"status": "success", "message": "机顶盒配置已保存并重载！", "ip": config_stb.ip_address}
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"保存配置失败: {str(e)}"}, status_code=500)

@app.post("/api/test-login")
async def test_login():
    try:
        sim.state.is_authenticated = False # Reset auth to test fresh
        success = sim.login()
        if success:
            return {"status": "success", "message": "模拟登录成功！EPG 网关已验证成功。"}
        else:
            return {"status": "error", "message": "模拟登录失败，请检查机顶盒凭证参数或网关连通性。"}
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"登录测试出现异常: {str(e)}"}, status_code=500)

@app.get("/api/vod-categories")
async def get_vod_categories():
    return load_vod_categories()

@app.post("/api/vod-categories")
async def save_vod_categories(request: Request):
    try:
        categories = await request.json()
        
        # Clean up: only keep the "sub_type" filter for each category
        for cat in categories:
            filters = cat.get("filters", [])
            cleaned_filters = []
            for filt in filters:
                if filt.get("key") == "sub_type":
                    cleaned_filters.append(filt)
                    break
            cat["filters"] = cleaned_filters

        save_data = {
            "version": 2,
            "categories": categories
        }
        with open(VOD_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        return {"status": "success", "message": "分类配置已保存！"}
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"保存失败: {str(e)}"}, status_code=500)

@app.get("/api/vod-filters")
async def get_vod_filters():
    cats = load_vod_categories()
    movies_filters = []
    series_filters = []
    for cat in cats:
        if cat.get("id") == "movies":
            movies_filters = cat.get("filters", [])
        elif cat.get("id") == "series":
            series_filters = cat.get("filters", [])
    return {
        "movies": movies_filters,
        "series": series_filters
    }

@app.post("/api/vod-filters")
async def save_vod_filters(filters: dict):
    try:
        movies_filters = filters.get("movies", [])
        series_filters = filters.get("series", [])
        
        cats = load_vod_categories()
        for cat in cats:
            if cat.get("id") == "movies":
                cat["filters"] = movies_filters
            elif cat.get("id") == "series":
                cat["filters"] = series_filters
                
        # Clean up: only keep the "sub_type" filter for each category
        for cat in cats:
            filters = cat.get("filters", [])
            cleaned_filters = []
            for filt in filters:
                if filt.get("key") == "sub_type":
                    cleaned_filters.append(filt)
                    break
            cat["filters"] = cleaned_filters

        save_data = {
            "version": 2,
            "categories": cats
        }
        with open(VOD_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
            
        return {"status": "success", "message": "分类配置已保存！"}
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"保存失败: {str(e)}"}, status_code=500)

@app.get("/api/vod-source-tree")
async def get_vod_source_tree():
    """Return cached category tree (with counts)."""
    if os.path.exists(VOD_SOURCE_TREE_CACHE_FILE):
        try:
            with open(VOD_SOURCE_TREE_CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            return JSONResponse(content={
                "tree": cache_data["tree"],
                "refreshing": source_tree_refresh_status["refreshing"],
                "done": source_tree_refresh_status["done"],
                "total": source_tree_refresh_status["total"],
                "last_updated": cache_data.get("updated_at"),
                "cached": True
            })
        except Exception as e:
            print(f">>> [Source Tree] Cache read error: {e}")

    return JSONResponse(content={
        "tree": [],
        "refreshing": source_tree_refresh_status["refreshing"],
        "done": source_tree_refresh_status["done"],
        "total": source_tree_refresh_status["total"],
        "last_updated": None,
        "cached": False
    })


@app.post("/api/vod-source-tree/refresh")
async def trigger_source_tree_refresh():
    """Kick off a background refresh from the Telecom VIS server."""
    if source_tree_refresh_status["refreshing"]:
        return JSONResponse(content={
            "status": "already_refreshing",
            "message": f"正在抓取中… ({source_tree_refresh_status['done']}/{source_tree_refresh_status['total']})",
            "done": source_tree_refresh_status["done"],
            "total": source_tree_refresh_status["total"]
        })

    thread = threading.Thread(target=refresh_source_tree_bg, daemon=True)
    thread.start()
    return {"status": "started", "message": "已开始从电信服务器抓取分类数据，请稍候查看进度…"}


@app.get("/api/vod-source-tree/status")
async def get_source_tree_status():
    """Return current refresh progress."""
    return JSONResponse(content=source_tree_refresh_status)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
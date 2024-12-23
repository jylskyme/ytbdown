from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import yt_dlp
import os
import json
from datetime import datetime
import subprocess
import platform
import time

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 定义目录常量
DOWNLOAD_DIR = os.path.abspath("downloads")
TEMPLATES_DIR = "templates"
STATIC_DIR = "static"

# 添加静态文件挂载
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/downloads", StaticFiles(directory=DOWNLOAD_DIR), name="downloads")

# 确保目录存在
for directory in [DOWNLOAD_DIR, TEMPLATES_DIR, STATIC_DIR]:
    os.makedirs(directory, exist_ok=True)

# 存储下载历史的文件
HISTORY_FILE = "download_history.json"

# 存储当前下载进度
download_progress = {}

def load_history(sort_by='date', order='desc'):
    if not os.path.exists(HISTORY_FILE):
        return []
    
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        history = json.load(f)
        
    # 根据不同的排序选项排序
    sort_keys = {
        'date': lambda x: x['download_date'],
        'size': lambda x: x['filesize'],
        'duration': lambda x: x['duration']
    }
    
    if sort_by in sort_keys:
        history.sort(key=sort_keys[sort_by], reverse=(order == 'desc'))
    
    return history

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def progress_hook(d):
    video_id = d['info_dict']['id']
    if d['status'] == 'downloading':
        try:
            # 获取下载信息
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes', 0)
            
            # 如果没有total_bytes，尝试其他来源
            if not total:
                total = d.get('total_bytes_estimate', 0)
            
            # 计算速度和剩余时间
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            
            # 计算进度百分比
            percent = (downloaded / total * 100) if total > 0 else 0
            
            # 更新下载进度
            download_progress[video_id] = {
                'status': 'downloading',
                'downloaded_bytes': downloaded,
                'total_bytes': total,
                'speed': speed,
                'eta': eta,
                'percent': percent
            }
            
            # 打印调试信息
            print(f"\nDownload progress for {video_id}:")
            print(f"Downloaded: {downloaded/1024/1024:.1f}MB")
            print(f"Total: {total/1024/1024:.1f}MB")
            
        except Exception as e:
            print(f"Error in progress_hook: {str(e)}")
            print("Debug info:", d)
    
    elif d['status'] == 'finished':
        try:
            # 这里只记录状态，实际大小在download_video函数中更新
            download_progress[video_id] = {
                'status': 'merging',  # 使用merging状态表示正在处理
                'downloaded_bytes': 0,
                'total_bytes': 0,
                'speed': 0,
                'eta': 0,
                'percent': 99  # 显示99%表示正在处理
            }
            print(f"Download finished, merging files...")
            
        except Exception as e:
            print(f"Error in progress_hook (finished): {str(e)}")
            print("Debug info:", d)

async def get_video_formats(url: str):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            formats = []
            
            # 获取所有视频格式
            for f in info['formats']:
                # 只获取视频格式
                if f.get('vcodec') != 'none':
                    format_info = {
                        'format_id': f['format_id'],
                        'ext': f['ext'],
                        'resolution': f.get('resolution', 'N/A'),
                        'filesize': f.get('filesize', 0),
                        'format_note': f.get('format_note', ''),
                        'fps': f.get('fps', ''),
                        'vcodec': f.get('vcodec', ''),
                        'acodec': f.get('acodec', 'none'),
                        'tbr': f.get('tbr', 0),  # 总比特率
                    }
                    
                    # 构建更详细的显示信息
                    format_note = []
                    if format_info['resolution'] != 'N/A':
                        format_note.append(format_info['resolution'])
                    if format_info['fps']:
                        format_note.append(f"{format_info['fps']}fps")
                    if format_info['tbr']:
                        format_note.append(f"{format_info['tbr']}kbps")
                    if format_info['format_note']:
                        format_note.append(format_info['format_note'])
                    
                    # 显示预计大小
                    size_mb = format_info['filesize'] / 1024 / 1024 if format_info['filesize'] else None
                    if size_mb:
                        format_note.append(f"约 {size_mb:.1f}MB")
                    
                    format_info['display_name'] = ' - '.join(format_note)
                    formats.append(format_info)
            
            # 按分辨率、fps和比特率排序
            formats.sort(key=lambda x: (
                int(x['resolution'].split('x')[1]) if x['resolution'] != 'N/A' else 0,
                x['fps'] or 0,
                x['tbr'] or 0
            ), reverse=True)
            
            # 添加几个预设选项
            presets = [
                {
                    'format_id': 'best',
                    'display_name': '最高质量 (可能较慢)',
                    'resolution': 'best',
                    'filesize': 0,
                },
                {
                    'format_id': 'best[height<=1080]',
                    'display_name': '1080p (推荐)',
                    'resolution': '1080p',
                    'filesize': 0,
                },
                {
                    'format_id': 'best[height<=720]',
                    'display_name': '720p (较快)',
                    'resolution': '720p',
                    'filesize': 0,
                }
            ]
            formats = presets + formats
            
            return {
                'title': info['title'],
                'formats': formats
            }
        except Exception as e:
            print(f"Error getting formats: {str(e)}")
            return None

async def download_video(url: str, format_id: str = None):
    ydl_opts = {
        'format': (f'{format_id}+bestaudio/best' if format_id and format_id != 'best' else 'bestvideo+bestaudio/best'),
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'merge_output_format': 'mp4',
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'nocheckcertificate': True,
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'ignoreerrors': True,
        'quiet': False,
        'no_warnings': False,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # 等待一小段时间确保文件完全写入
            time.sleep(2)
            
            # 获取实际文件大小
            actual_size = os.path.getsize(filename)
            print(f"Final file check - Size: {actual_size/1024/1024:.1f}MB")
            
            # 获取YouTube缩略图URL
            thumbnail_url = info.get('thumbnail') or (
                info.get('thumbnails', [{}])[-1].get('url') if info.get('thumbnails') else None
            )
            
            video_data = {
                'id': info['id'],
                'title': info['title'],
                'duration': info['duration'],
                'uploader': info['uploader'],
                'description': info['description'],
                'filename': os.path.basename(filename),
                'thumbnail_url': thumbnail_url,
                'download_date': datetime.now().isoformat(),
                'filesize': actual_size,  # 使用实际文件大小
                'url': url
            }
            
            # 更新最终的下载进度
            download_progress[info['id']] = {
                'status': 'finished',
                'downloaded_bytes': actual_size,
                'total_bytes': actual_size,
                'speed': 0,
                'eta': 0,
                'percent': 100
            }
            
            history = load_history()
            history.append(video_data)
            save_history(history)
            
            return video_data
            
        except Exception as e:
            print(f"Download error: {str(e)}")
            return None

@app.get("/")
async def home(request: Request, sort_by: str = 'date', order: str = 'desc'):
    history = load_history(sort_by, order)
    return templates.TemplateResponse(
        "index.html", 
        {"request": request, "videos": history, "sort_by": sort_by, "order": order}
    )

@app.post("/download")
async def start_download(url: str, background_tasks: BackgroundTasks, format_id: str = None):
    background_tasks.add_task(download_video, url, format_id)
    return {"message": "Download started"}

@app.get("/progress/{video_id}")
async def get_progress(video_id: str):
    return download_progress.get(video_id, {'status': 'not_found'})

@app.get("/videos")
async def get_videos(sort_by: str = 'date', order: str = 'desc'):
    return load_history(sort_by, order)

@app.get("/formats")
async def get_formats(url: str):
    formats = await get_video_formats(url)
    if formats:
        return formats
    return {"error": "Failed to get video formats"}

@app.post("/delete/{video_id}")
async def delete_video(video_id: str):
    history = load_history()
    video = next((v for v in history if v['id'] == video_id), None)
    if video:
        try:
            # 使用完整路径删除文件
            file_path = os.path.join(DOWNLOAD_DIR, video['filename'])
            if os.path.exists(file_path):
                os.remove(file_path)
            # 从历史记录中删除
            history = [v for v in history if v['id'] != video_id]
            save_history(history)
            return {"message": "Video deleted successfully"}
        except Exception as e:
            print(f"Error deleting video: {str(e)}")
            return {"error": f"Failed to delete video: {str(e)}"}
    return {"error": "Video not found"}

@app.get("/open_folder/{video_id}")
async def open_folder(video_id: str):
    history = load_history()
    video = next((v for v in history if v['id'] == video_id), None)
    if video:
        try:
            # 使用完整路径
            folder_path = os.path.dirname(os.path.join(DOWNLOAD_DIR, video['filename']))
            if platform.system() == "Windows":
                os.startfile(folder_path)
            elif platform.system() == "Darwin":  # macOS
                subprocess.run(["open", folder_path])
            else:  # Linux
                subprocess.run(["xdg-open", folder_path])
            return {"message": "Folder opened successfully"}
        except Exception as e:
            print(f"Error opening folder: {str(e)}")
            return {"error": f"Failed to open folder: {str(e)}"}
    return {"error": "Video not found"}

@app.get("/debug/video/{video_id}")
async def debug_video(video_id: str):
    history = load_history()
    video = next((v for v in history if v['id'] == video_id), None)
    if video:
        return {
            "video_info": video,
            "file_exists": os.path.exists(os.path.join(DOWNLOAD_DIR, video['filename'])),
            "absolute_path": os.path.abspath(os.path.join(DOWNLOAD_DIR, video['filename'])),
            "download_dir": os.path.abspath(DOWNLOAD_DIR),
            "file_size": os.path.getsize(os.path.join(DOWNLOAD_DIR, video['filename'])) if os.path.exists(os.path.join(DOWNLOAD_DIR, video['filename'])) else None
        }
    return {"error": "Video not found"}

@app.get("/debug/files")
async def debug_files():
    """列出downloads目录中的所有文件及其信息"""
    files = []
    for filename in os.listdir(DOWNLOAD_DIR):
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        files.append({
            "name": filename,
            "exists": os.path.exists(filepath),
            "size": os.path.getsize(filepath) if os.path.exists(filepath) else None,
            "path": filepath,
            "is_file": os.path.isfile(filepath)
        })
    return {
        "download_dir": DOWNLOAD_DIR,
        "files": files,
        "dir_exists": os.path.exists(DOWNLOAD_DIR),
        "dir_is_absolute": os.path.isabs(DOWNLOAD_DIR)
    }
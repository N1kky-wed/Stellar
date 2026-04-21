import time
import uuid
import requests
import json
import base64
import os
import re
from tavily import TavilyClient
import asyncio
from google import genai
from google.genai import types

import logging
logger = logging.getLogger(__name__)

# Pre-compiled regex patterns for SVG markdown stripping
SVG_START_RE = re.compile(r'^```(?:svg|xml)?\s*', re.IGNORECASE)
SVG_END_RE = re.compile(r'\s*```$')

def extensive_search(query: str, status: str, topic: str = "general", days: int = 3, max_results: int = 10) -> str:
    """Performs a deep web search using Tavily API.
    Args:
        query: The search query
        status: Status update for the user.
        topic: 'general' or 'news'
        days: for news, how many days back
        max_results: number of results to return
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Error: Tavily API key not found."
    
    client = TavilyClient(api_key=api_key)
    try:
        response = client.search(query=query, topic=topic, days=days, max_results=max_results, search_depth="advanced")
        results = response.get('results', [])
        if not results:
            return "No results found."
        
        formatted = "### Search Results:\n\n"
        from bs4 import BeautifulSoup
        import re
        for r in results:
            content = r.get('content', '')
            if content:
                content = BeautifulSoup(content, 'html.parser').get_text(separator=' ', strip=True)
                content = re.sub(r'\s+', ' ', content)
            
            formatted += f"**[{r.get('title', 'Unknown Title')}]({r.get('url', '#')})**\n"
            formatted += f"{content}\n\n"
        return formatted
    except Exception as e:
        return f"Error during search: {str(e)}"

def generate_image(model: str, prompt: str, status: str, quality: str = "1K", aspect_ratio: str = "1:1", reference_images: list[str] = None) -> str:
    """Generates an image using Gemini's Imagen model.
    Args:
        model: 'gemini-3.1-flash-image-preview' or 'gemini-3-pro-image-preview'
        prompt: detailed descriptive prompt for the image
        status: Status update for the user.
        quality: Supported tiers are "512", "1K", "2K", "4K". (Default: "1K")
        aspect_ratio: Supported ratios: '1:1', '3:4', '4:3', '9:16', '16:9'.
        reference_images: List of filenames from the chat context to use as reference/conditioning (up to 14).
    """
    from app import PRIMARY_API_KEY, UPLOAD_FOLDER
    from flask import session
    import os
    import mimetypes
    import uuid
    
    client = genai.Client(api_key=PRIMARY_API_KEY)
    
    # Ensure aspect_ratio is one of the strictly supported API values
    valid_ratios = ["1:1", "3:4", "4:3", "9:16", "16:9"]
    if aspect_ratio not in valid_ratios:
        aspect_ratio = "1:1"
        
    quality_lower = quality.lower()
    if "4k" in quality_lower or "high" in quality_lower or "hd" in quality_lower:
        img_size = "4K"
    elif "2k" in quality_lower:
        img_size = "2K"
    elif "1k" in quality_lower or "standard" in quality_lower:
        img_size = "1K"
    elif "512" in quality_lower:
        img_size = "512"
    else:
        img_size = "1K"
    
    image_config_args = {"aspect_ratio": aspect_ratio, "image_size": img_size}
    
    # Resolve reference images
    parts = [types.Part.from_text(text=prompt)]
    if reference_images:
        try:
            session_id = session.sid
            local_dir = os.path.join(UPLOAD_FOLDER, session_id)
            for img_name in reference_images[:14]:
                img_path = os.path.join(local_dir, img_name)
                if os.path.exists(img_path):
                    mime_type, _ = mimetypes.guess_type(img_path)
                    with open(img_path, "rb") as f:
                        parts.append(types.Part.from_bytes(data=f.read(), mime_type=mime_type or "image/png"))
        except Exception as e:
            logger.error(f"Error loading reference images: {e}")

    try:
        response = client.models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(
                image_config=types.ImageConfig(**image_config_args),
                response_modalities=["IMAGE"]
            )
        )
        
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                img_data = part.inline_data.data
                
                output_dir = "outputs"
                os.makedirs(output_dir, exist_ok=True)
                
                mime_type = getattr(part.inline_data, 'mime_type', 'image/png')
                ext = "png"
                if "jpeg" in mime_type: ext = "jpg"
                elif "webp" in mime_type: ext = "webp"
                
                filename = f"gen_{uuid.uuid4().hex[:8]}.{ext}"
                file_path = os.path.join(output_dir, filename)
                
                with open(file_path, "wb") as f:
                    f.write(img_data)
                
                return f"![Generated Image](https://stellarai.live/view/{filename})"
        return "Image model returned no visual data."
    except Exception as e:
        return f"Error generating image: {str(e)}"

def native_search(prompt: str, status: str) -> str:
    """Uses gemini-2.5-flash-lite with Google Search tool enabled to search the web and return the result.
    Args:
        prompt: A fully self-contained search query to send to Google. Never use pronouns like 'it' or 'that', specify exactly what you are looking for.
        status: Status update for the user.
    """
    import datetime
    from app import PRIMARY_API_KEY
    try:
        current_date = datetime.datetime.now().strftime('%A, %B %d, %Y')
        client = genai.Client(api_key=PRIMARY_API_KEY)
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=f"Today's date is {current_date}.",
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        return response.text
    except Exception as e:
        return f"Error in native search: {str(e)}"

def logs_and_preferences(status: str, write: str = "", user_id: str = "global") -> str:
    """Stores user preferences, previous errors, and resolution strategies. Memory is automatically provided to your context.
    
    Args:
        status: Status update for the user.
        write: A string detailing a preference, error, or fix to save for the future.
    """
    import sqlite3
    from app import DATABASE_NAME
    
    try:
        if not write:
            return "Error: You must provide a 'write' string to save a preference."

        write = write.strip()
        if not write:
            return "Error: 'write' string was empty."

        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        
        # Insert new log
        cursor.execute('INSERT INTO user_logs_prefs (user_id, log_entry) VALUES (?, ?)', (user_id, write))
        
        # Keep only the last 100 entries for this user to prevent bloat
        cursor.execute('''
            DELETE FROM user_logs_prefs 
            WHERE id IN (
                SELECT id FROM user_logs_prefs 
                WHERE user_id = ? 
                ORDER BY created_at DESC 
                LIMIT -1 OFFSET 100
            )
        ''', (user_id,))
        
        conn.commit()
        conn.close()
        
        return "Successfully saved to logs/preferences. It will be available in your context for future turns."
    except Exception as e:
        return f"Error accessing logs/preferences: {str(e)}"

def make_presentation(topic: str, status: str, num_slides: int = 10, style: str = "corporate", additional_context: str = "") -> str:
    """Generate a fully designed PowerPoint presentation where each slide is a full-bleed AI generated image containing text.
    Args:
        topic: The topic of the presentation
        status: Status update for the user.
        num_slides: Number of slides
        style: descriptive style for the presentation (e.g. "educational infographic", "minimalist executive", "dark technical documentation")
        additional_context: detailed research information to include in the slides
    """
    import asyncio
    import threading
    import json
    import os
    import uuid
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from io import BytesIO
    from app import PRIMARY_API_KEY
    from pydantic import BaseModel, Field

    class Slide(BaseModel):
        title: str = Field(description="Main title for the slide.")
        summary: str = Field(description="A comprehensive, detailed summary for the slide. Provide as much informative content as necessary to make the slide educational and deep, using multiple paragraphs or extensive bullet points if needed.")
        background_description: str = Field(description="Detailed description of the visual layout: specify a multi-column infographic structure, specific diagrams (like flowcharts or state diagrams), icons, and thematic imagery that complements the dense text.")

    class PresentationPlan(BaseModel):
        slides: list[Slide]

    client = genai.Client(api_key=PRIMARY_API_KEY)
    slide_plan_prompt = (
        f"Plan {num_slides} professional infographic-style slides for a presentation on '{topic}'.\n"
        f"Style: {style}.\n"
        f"Context: {additional_context}.\n"
        "Each slide should be designed as a complete visual experience. Include specific sections, diagrams, or icons in the background description."
    )
    
    try:
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=slide_plan_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PresentationPlan
            )
        )
        plan = json.loads(resp.text)
        slides_data = plan.get('slides', [])
    except Exception as e:
        return f"Failed to plan presentation: {str(e)}"
    
    async def fetch_image(slide_data):
        try:
            full_image_prompt = (
                f"A professional, high-resolution 16:9 presentation infographic slide. Style: '{style}'.\n"
                f"LAYOUT: {slide_data.get('background_description')}.\n"
                f"CONTENT TO RENDER DIRECTLY IN IMAGE:\n"
                f"Main Header: '{slide_data.get('title')}'\n"
                f"Body Text: '{slide_data.get('summary')}'\n"
                "INSTRUCTIONS:\n"
                "- Use professional, clean sans-serif typography.\n"
                "- Integrate the text aesthetically into a multi-column or structured infographic layout.\n"
                "- Include relevant icons, charts, or transition diagrams if mentioned in the layout description.\n"
                "- The result must be a single, complete, polished slide design. No watermarks. Legible text."
            )
            
            loop = asyncio.get_event_loop()
            def gen_sync():
                return client.models.generate_content(
                    model='gemini-3.1-flash-image-preview',
                    contents=full_image_prompt,
                )
            result = await loop.run_in_executor(None, gen_sync)
            if result.candidates and result.candidates[0].content.parts:
                for part in result.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        return part.inline_data.data
            return None
        except:
            return None

    async def fetch_all_images():
        tasks = [fetch_image(slide) for slide in slides_data]
        return await asyncio.gather(*tasks)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    images = loop.run_until_complete(fetch_all_images())

    presentation_id = uuid.uuid4().hex[:8]
    output_dir = "outputs"
    pres_dir = os.path.join(output_dir, f"pres_{presentation_id}")
    os.makedirs(pres_dir, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slide_image_urls = []
    for i, img_bytes in enumerate(images):
        blank_layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank_layout)
        
        slide_img_filename = f"slide_{i+1}.png"
        slide_img_path = os.path.join(pres_dir, slide_img_filename)
        
        if img_bytes:
            with open(slide_img_path, "wb") as f:
                f.write(img_bytes)
            slide_image_urls.append(f"/view/pres_{presentation_id}/{slide_img_filename}")
            
            image_stream = BytesIO(img_bytes)
            slide.shapes.add_picture(image_stream, 0, 0, width=prs.slide_width, height=prs.slide_height)
        else:
            title_shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(11), Inches(1)).text_frame
            title_shape.text = slides_data[i].get('title', f"Slide {i+1}")
            content_shape = slide.shapes.add_textbox(Inches(1), Inches(2.5), Inches(11), Inches(4)).text_frame
            content_shape.text = slides_data[i].get('summary', "")
            slide_image_urls.append(None)

    pptx_filename = f"presentation_{presentation_id}.pptx"
    pptx_filepath = os.path.join(output_dir, pptx_filename)
    prs.save(pptx_filepath)
    
    result_data = {
        "presentation_id": presentation_id,
        "pptx_url": f"/download/{pptx_filename}",
        "slides": slide_image_urls,
        "topic": topic,
        "style": style,
        "num_slides": num_slides,
        "additional_context": additional_context
    }
    
    return f"PRESENTATION_DATA:{json.dumps(result_data)}"

def regenerate_presentation_slide(presentation_id: str, slide_index: int, status: str, topic: str = "", style: str = "", additional_context: str = "", feedback: str = "") -> str:
    """Regenerate a specific slide of an existing presentation based on feedback.
    Args:
        presentation_id: the ID of the presentation
        slide_index: 0-based index of the slide to regenerate
        status: Status update for the user.
        topic: original topic
        style: original style
        additional_context: original context
        feedback: specific feedback for this slide's improvement
    """
    import os
    import json
    import asyncio
    from pptx import Presentation
    from pptx.util import Inches
    from io import BytesIO
    from app import PRIMARY_API_KEY
    from pydantic import BaseModel, Field

    client = genai.Client(api_key=PRIMARY_API_KEY)
    
    # Load existing slide image for reference if it exists
    existing_image_part = None
    try:
        pres_dir = os.path.join("outputs", f"pres_{presentation_id}")
        slide_path = os.path.join(pres_dir, f"slide_{slide_index + 1}.png")
        if os.path.exists(slide_path):
            with open(slide_path, "rb") as f:
                img_data = f.read()
                existing_image_part = {
                    'mime_type': 'image/png',
                    'data': img_data
                }
    except:
        pass

    # Re-plan only this slide
    slide_plan_prompt = (
        f"Analyze the attached image (Slide {slide_index + 1} of presentation {presentation_id}) and re-plan it based on this feedback: '{feedback}'.\n"
        f"Original Topic: {topic}.\n"
        f"Target Style: {style}.\n"
        "Instructions:\n"
        "1. Identify what is currently on the slide.\n"
        "2. Apply the user feedback to create a NEW plan.\n"
        "3. Ensure the new design is a complete visual experience. Include specific sections, diagrams, or 3D elements in the background description as requested."
    )

    class Slide(BaseModel):
        title: str = Field(description="Main title for the slide.")
        summary: str = Field(description="A comprehensive, detailed summary for the slide.")
        background_description: str = Field(description="Detailed description of the visual layout.")

    try:
        contents = [slide_plan_prompt]
        if existing_image_part:
            contents.append(types.Part.from_bytes(data=existing_image_part['data'], mime_type=existing_image_part['mime_type']))

        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Slide
            )
        )
        slide_data = json.loads(resp.text)
    except Exception as e:
        return f"Failed to re-plan slide: {str(e)}"

    # Generate image for this slide
    full_image_prompt = (
        f"Using the attached image as a reference, modify it based on this feedback: '{feedback}'.\n"
        f"Target Infographic Layout: {slide_data.get('background_description')}.\n"
        f"CONTENT TO RENDER:\n"
        f"Main Header: '{slide_data.get('title')}'\n"
        f"Body Text: '{slide_data.get('summary')}'\n"
        "INSTRUCTIONS:\n"
        "- Maintain the professional style: '{style}'.\n"
        "- Ensure high-resolution, clean typography, and 3D infographics if requested.\n"
        "- The result must be a single, complete, polished slide design."
    )

    try:
        image_contents = [full_image_prompt]
        if existing_image_part:
            image_contents.append(types.Part.from_bytes(data=existing_image_part['data'], mime_type=existing_image_part['mime_type']))

        result = client.models.generate_content(
            model='gemini-3.1-flash-image-preview',
            contents=image_contents,
        )
        img_bytes = None
        if result.candidates and result.candidates[0].content.parts:
            for part in result.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    img_bytes = part.inline_data.data
                    break
    except Exception as e:
        return f"Failed to generate slide image: {str(e)}"

    if not img_bytes:
        return "Failed to generate image bytes."

    output_dir = "outputs"
    pres_dir = os.path.join(output_dir, f"pres_{presentation_id}")
    os.makedirs(pres_dir, exist_ok=True)
    
    slide_img_filename = f"slide_{slide_index+1}.png"
    slide_img_path = os.path.join(pres_dir, slide_img_filename)
    with open(slide_img_path, "wb") as f:
        f.write(img_bytes)
        
    # Update PPTX if it exists
    pptx_filename = f"presentation_{presentation_id}.pptx"
    pptx_filepath = os.path.join(output_dir, pptx_filename)
    if os.path.exists(pptx_filepath):
        try:
            prs = Presentation(pptx_filepath)
            if slide_index < len(prs.slides):
                slide = prs.slides[slide_index]
                for shape in list(slide.shapes):
                    sp = shape._element
                    sp.getparent().remove(sp)
                
                image_stream = BytesIO(img_bytes)
                slide.shapes.add_picture(image_stream, 0, 0, width=prs.slide_width, height=prs.slide_height)
                prs.save(pptx_filepath)
        except Exception as e:
            print(f"Error updating PPTX: {e}")

    return f"REGENERATED_SLIDE:{json.dumps({'presentation_id': presentation_id, 'slide_index': slide_index, 'url': f'/view/pres_{presentation_id}/{slide_img_filename}'})}"


def forge_control(action: str, status: str, app_id: str = None, changes: dict = None, prompt: str = None, project_name: str = None) -> str:
    """Control the user's Forge deployments.
    Args:
        action: "list_history", "read_files", "create", or "modify"
        status: Status update for the user.
        app_id: the Forge application identifier, Project Title, or Subdomain (required for 'read_files' and 'modify')
        changes: key-value config/code changes to apply (e.g. {'app.py': '...'})
        prompt: Instruction for AI-driven modification or creation
        project_name: Optional name for a new project (if omitted, one is generated)
    Returns:
        deployment status, history list, source code, or live URL
    """
    from app import get_current_session_id, get_db, ERROR_CODE, gemini_generate, _extract_json_from_response, generate_forge_title, _redis_forge_key
    from prompts import get_forge_initial_build_prompt, get_forge_iteration_prompt
    from flask import session, current_app
    import os
    import json
    import uuid
    import threading
    
    try:
        if action == "list_history":
            if 'user_id' not in session:
                return "Error: Authentication required."
            db = get_db()
            # Group by process_id and take the latest iteration (max id)
            cursor = db.execute('''
                SELECT fh.project_name, fh.process_id, fh.status, fh.deployment_url, fh.subdomain, fh.created_at 
                FROM forge_history fh
                INNER JOIN (
                    SELECT process_id, MAX(id) as latest_id
                    FROM forge_history
                    WHERE user_id = ?
                    GROUP BY process_id
                ) latest ON fh.id = latest.latest_id
                ORDER BY fh.created_at DESC
            ''', (session['user_id'],))
            history = cursor.fetchall()
            if not history:
                return "You have no past Forge deployments."
            
            res = "### Your Forge Deployment History:\n"
            for row in history:
                if row['status'] == 'running':
                    if row['deployment_url']:
                        url = row['deployment_url']
                    elif row['subdomain']:
                        url = f"https://{row['subdomain']}.stellarai.live/"
                    else:
                        url = f"https://{row['process_id']}.stellarai.live/"
                    url_str = f" - [Visit App]({url})"
                else:
                    url_str = ""
                res += f"- **{row['project_name']}** (ID: `{row['process_id']}`) - Status: {row['status']} - Created: {row['created_at']}{url_str}\n"
            return res

        actual_app_id = None
        project_title = None
        current_files = {}
        old_container_id = None
        db = get_db()

        if action == "read_files":
            if not app_id:
                return "Error: app_id, Project Title, or Subdomain is required to read files."
            
            # Resolve the project (checking Title, ID, or Subdomain)
            cursor = db.execute('SELECT project_name, files_snapshot FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            
            if not row:
                return f"Error: Project '{app_id}' not found."

            project_name = row['project_name']
            files = json.loads(row['files_snapshot'])
            
            output = f"### Source Code for Project: {project_name}\n"
            for filename, content in files.items():
                output += f"\n**File: {filename}**\n```\n{content}\n```\n"
            
            return output

        if action == "rename":
            if not app_id or not project_name:
                return "Error: Both 'app_id' (current project) and 'project_name' (new name) are required for rename."
            
            cursor = db.execute('SELECT process_id, project_name, subdomain FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Project '{app_id}' not found."
            
            actual_app_id = row['process_id']
            from app import generate_unique_subdomain
            new_subdomain = generate_unique_subdomain(project_name)
            new_url = f"https://{new_subdomain}.stellarai.live/"
            
            db.execute("UPDATE forge_history SET project_name = ?, subdomain = ?, deployment_url = ? WHERE process_id = ?", (project_name, new_subdomain, new_url, actual_app_id))
            db.commit()
            
            return f"Project renamed to '{project_name}'! New URL: {new_url}"

        if action == "create":
            if not prompt:
                return "Error: A prompt is required to create a new Forge project."
            if 'user_id' not in session:
                return "Error: Authentication required."
                
            model_id = "gemini-3.1-pro-preview"
            from app import PRIMARY_API_KEY
            build_prompt = get_forge_initial_build_prompt(prompt)
            generator = gemini_generate(build_prompt, model_id, PRIMARY_API_KEY)
            raw_response = "".join([item['result'] for item in generator if 'result' in item])
            
            if not raw_response or raw_response.startswith(ERROR_CODE):
                return f"Error: Failed to generate code. {raw_response}"
                
            clean_json_string = _extract_json_from_response(raw_response)
            if not clean_json_string:
                return "Error: AI failed to return valid project files."
                
            current_files = json.loads(clean_json_string)
            actual_app_id = str(uuid.uuid4())
            project_title = project_name if project_name else generate_forge_title(prompt)
            from app import generate_unique_subdomain
            subdomain = generate_unique_subdomain(project_title)
            
            session['forge_project'] = {'files': current_files, 'container_id': None, 'process_id': actual_app_id, 'project_name': project_title, 'subdomain': subdomain}
            session.modified = True
            
            db.execute('INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot, subdomain) VALUES (?, ?, ?, ?, ?, ?)',
                       (session['user_id'], project_title, actual_app_id, 'starting', json.dumps(current_files), subdomain))
            db.commit()

        elif action == "modify":
            if not app_id:
                return "Error: app_id or Project Title is required for modification."
            
            # Resolve title/ID/subdomain with fuzzy matching
            cursor = db.execute('SELECT process_id, project_name, files_snapshot, subdomain FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            
            if not row:
                fuzzy_query = f"%{app_id}%"
                cursor = db.execute('SELECT process_id, project_name, files_snapshot, subdomain FROM forge_history WHERE (project_name LIKE ? OR subdomain LIKE ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (fuzzy_query, fuzzy_query, session.get('user_id')))
                row = cursor.fetchone()

            if not row:
                cursor = db.execute('SELECT project_name, process_id, status, created_at FROM forge_history WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
                history = cursor.fetchall()
                if not history: return f"Error: Project '{app_id}' not found and you have no past Forge deployments."
                res = f"Error: Project '{app_id}' not found in your history. Existing projects:\n"
                for r in history: res += f"- **{r['project_name']}** (ID: `{r['process_id']}`) - Status: {r['status']}\n"
                return res
            
            actual_app_id = row['process_id']
            project_title = row['project_name']
            subdomain = row['subdomain']
            current_files = json.loads(row['files_snapshot'])
            
            if prompt:
                model_id = "gemini-3.1-pro-preview"
                from app import PRIMARY_API_KEY
                iter_prompt = get_forge_iteration_prompt(prompt, json.dumps(current_files))
                generator = gemini_generate(iter_prompt, model_id, PRIMARY_API_KEY)
                raw_response = "".join([item['result'] for item in generator if 'result' in item])
                if not raw_response or raw_response.startswith(ERROR_CODE): return f"Error: AI iteration failed. {raw_response}"
                clean_json_string = _extract_json_from_response(raw_response)
                if clean_json_string: current_files.update(json.loads(clean_json_string))
            
            if changes: current_files.update(changes)
                
            session['forge_project'] = {'files': current_files, 'container_id': None, 'process_id': actual_app_id, 'project_name': project_title, 'subdomain': subdomain}
            session.modified = True
            db.execute('UPDATE forge_history SET files_snapshot = ?, status = ? WHERE process_id = ?', (json.dumps(current_files), 'starting', actual_app_id))
            db.commit()

            from app import redis_client, active_apps, active_apps_lock
            try:
                cached_data = redis_client.hgetall(_redis_forge_key(actual_app_id))
                if cached_data:
                    old_container_id = cached_data.get(b'container_id') or cached_data.get('container_id')
                    if isinstance(old_container_id, bytes): old_container_id = old_container_id.decode('utf-8')
            except: pass
            with active_apps_lock: active_apps.pop(actual_app_id, None)

        else:
            return f"Error: Unknown action '{action}'."

        # Trigger Deployment
        from app import _deploy_and_stream_output, redis_client
        try: redis_client.hset(_redis_forge_key(actual_app_id), mapping={"status": "starting", "files": json.dumps(current_files)})
        except: pass
        
        app_obj = current_app._get_current_object()
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, current_files, actual_app_id, old_container_id, 'forge', subdomain), daemon=True)
        thread.start()

        # Shared Wait Loop
        start_wait = time.time()
        final_status = "starting"
        public_url = f"https://{subdomain}.stellarai.live/" if 'subdomain' in locals() and subdomain else f"https://{actual_app_id}.stellarai.live/"
        
        while time.time() - start_wait < 35:
            time.sleep(2)
            try:
                data = redis_client.hgetall(_redis_forge_key(actual_app_id))
                if data:
                    final_status = data.get('status', 'starting')
                    if final_status in ['running', 'failed', 'stopped', 'exited']: break
            except: pass
        
        msg_prefix = f"Project '{project_title}'"
        if action == "modify":
            msg_prefix = f"Modification applied to '{project_title}'" if (prompt or changes) else f"Redeploying '{project_title}'"
        
        if final_status == 'running':
            return f"{msg_prefix} successfully! ID: `{actual_app_id}`. Live URL: {public_url}"
        elif final_status == 'failed':
            cursor = db.execute('SELECT build_logs FROM forge_history WHERE process_id = ?', (actual_app_id,))
            row = cursor.fetchone()
            logs = row['build_logs'] if row and row['build_logs'] else "No logs available."
            return f"Error: {msg_prefix} failed during health checks.\n\nBUILD/APP LOGS:\n{logs}\n\nID: `{actual_app_id}`. Please analyze the logs and use action='modify' to fix the code."
        else:
            return f"{msg_prefix} started! It is still initializing (Current status: {final_status}). Check it in a few moments: {public_url}"

    except Exception as e:
        return f"Error in forge_control: {str(e)}"

def repo_control(action: str, status: str, app_id: str = None, project_name: str = None, files: list[str] = None, repo_url: str = None, port: int = 3000, command: str = None) -> str:
    """Control and manage repository-based or custom-stack deployments.
    Args:
        action: "deploy", "execute", "list_history", "rename", "stop", "restart", or "snapshot"
        status: Status update for the user.
        app_id: the Deployment ID, Project Title, or Subdomain (required for all actions except 'deploy' and 'list_history')
        project_name: Custom name for the project (used for unique subdomain in 'deploy' and 'rename')
        files: List of file paths to save into the database (required for 'snapshot')
        repo_url: URL to a git repository (optional for 'deploy')
        port: Internal port the app will listen on (default 3000, used in 'deploy')
        command: Bash command to run (required for 'execute')
    Returns:
        status message, history list, or command output
    """
    from app import get_db, generate_unique_subdomain, stop_and_cleanup_app_by_process_id, _redis_forge_key, redis_client, active_apps, active_apps_lock
    from flask import session, current_app
    import json
    import docker
    import time
    import threading
    import subprocess
    import base64
    import os

    try:
        db = get_db()
        
        if action == "deploy":
            if 'user_id' not in session: return "Error: Authentication required to host repos."
            process_id = str(uuid.uuid4())
            if project_name: project_title = project_name
            elif repo_url: project_title = f"Repo: {repo_url.split('/')[-1].replace('.git', '')}"
            else: project_title = "Custom Stack Project"
            
            subdomain = generate_unique_subdomain(project_title)
            db.execute('INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot, subdomain) VALUES (?, ?, ?, ?, ?, ?)',
                       (session['user_id'], project_title, process_id, 'created', json.dumps({"repo": repo_url, "port": port} if repo_url else {"port": port}), subdomain))
            db.commit()

            try:
                client = docker.from_env()
                from app import ensure_user_network
                user_network = ensure_user_network(client, session['user_id'])
                r_key = _redis_forge_key(process_id)
                container = client.containers.run(
                    image='stellar-repo-host:latest',
                    command='sleep infinity',
                    ports={f"{port}/tcp": ('0.0.0.0', 0)},
                    name=f"stellar-repo-{process_id}",
                    remove=False,
                    detach=True,
                    init=True,
                    network=user_network,
                    stdout=True,
                    stderr=True,
                    working_dir='/app',
                    labels={
                        "stellar_type": "forge",
                        "stellar_process_id": process_id,
                        "created_at_ts": str(time.time()),
                        "forge_app_id": process_id,
                        "subdomain": subdomain
                    }
                )
                time.sleep(2)
                container.reload()
                host_port = container.attrs['NetworkSettings']['Ports'][f"{port}/tcp"][0]['HostPort']
                redis_client.hset(r_key, mapping={"container_id": container.id, "status": "running", "process_id": process_id, "host_port": str(host_port), "files": json.dumps({"repo": repo_url, "port": port} if repo_url else {"port": port})})
                with active_apps_lock: active_apps[process_id] = {"container_id": container.id, "port": host_port, "status": "running"}
                db.execute("UPDATE forge_history SET status = 'running', deployment_url = ? WHERE process_id = ?", (f"https://{subdomain}.stellarai.live/", process_id))
                db.commit()
                if repo_url:
                    clone_res = container.exec_run(f"git clone {repo_url} .", workdir="/app")
                    if clone_res.exit_code != 0: return f"Git clone failed: {clone_res.output.decode()}"
                
                public_url = f"https://{subdomain}.stellarai.live/"
                return f"Container provisioned for '{project_title}'! ID: `{process_id}`. Live URL: {public_url} Use action='execute' to build and start the app. CRITICAL: Ensure your app listens on 0.0.0.0 and port {port}."
            except Exception as e:
                db.execute("UPDATE forge_history SET status = 'failed' WHERE process_id = ?", (process_id,))
                db.commit()
                return f"Error provisioning container: {str(e)}"

        if action == "execute":
            if not app_id or not command: return "Error: 'app_id' and 'command' are required for execute."
            cursor = db.execute('SELECT process_id, files_snapshot FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Deployment '{app_id}' not found."
            p_id = row['process_id']
            snapshot = json.loads(row['files_snapshot'])
            port = snapshot.get('port', 3000)
            
            try:
                client = docker.from_env()
                container = client.containers.get(f"stellar-repo-{p_id}")
                wrapped_cmd = f"bash -c {subprocess.list2cmdline([command])}"
                exec_result = container.exec_run(wrapped_cmd, demux=False, workdir="/app")
                output = exec_result.output.decode('utf-8', 'replace')
                
                # Enhanced health check if a start-like command is detected
                if any(kw in command.lower() for kw in ["npm start", "python", "node", "serve", "go run", "npm run dev"]):
                    time.sleep(5)
                    container.reload()
                    if container.status != 'running':
                        return f"Command executed, but container stopped. Output:\n{output}"
                    
                    # Verify port accessibility and status
                    status_code = 0
                    for _ in range(5):
                        time.sleep(2)
                        res = container.exec_run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/")
                        if res.exit_code == 0:
                            status_code = int(res.output.decode().strip())
                            if 0 < status_code < 500:
                                break
                    
                    if 0 < status_code < 500:
                        output += f"\n\n✨ Server is READY (Status {status_code}) and listening on 0.0.0.0:{port}!"
                    elif status_code >= 500:
                        output += f"\n\n❌ Server responded with ERROR {status_code} on port {port}. Check your logs!"
                    else:
                        output += f"\n\n⚠️ Command run, but server is NOT responding on port {port}. Ensure it listens on 0.0.0.0."

                if exec_result.exit_code != 0: return f"Command failed (exit {exec_result.exit_code}).\nOutput:\n{output}"
                return output if output else "Command executed successfully with no output."
            except Exception as e: return f"Error executing in repo: {str(e)}"

        if action == "list_history":
            if 'user_id' not in session: return "Error: Authentication required."
            cursor = db.execute('SELECT project_name, process_id, status, deployment_url, subdomain, created_at FROM forge_history WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
            history = cursor.fetchall()
            if not history: return "You have no past deployments."
            res = "### Your Deployment History:\n"
            for row in history:
                url = row['deployment_url'] or (f"https://{row['subdomain']}.stellarai.live/" if row['subdomain'] else f"https://{row['process_id']}.stellarai.live/")
                url_str = f" - [Visit App]({url})" if row['status'] == 'running' else ""
                res += f"- **{row['project_name']}** (ID: `{row['process_id']}`) - Status: {row['status']} - Created: {row['created_at']}{url_str}\n"
            return res

        if action == "rename":
            if not app_id or not project_name:
                return "Error: Both 'app_id' (current project) and 'project_name' (new name) are required for rename."
            
            cursor = db.execute('SELECT process_id, project_name FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Deployment '{app_id}' not found."
            
            actual_id = row['process_id']
            new_subdomain = generate_unique_subdomain(project_name)
            new_url = f"https://{new_subdomain}.stellarai.live/"
            
            db.execute("UPDATE forge_history SET project_name = ?, subdomain = ?, deployment_url = ? WHERE process_id = ?", (project_name, new_subdomain, new_url, actual_id))
            db.commit()
            return f"Deployment renamed to '{project_name}'! New URL: {new_url}"

        def _perform_snapshot(p_id, container_name, current_snapshot):
            try:
                client = docker.from_env()
                container = client.containers.get(container_name)
                # We identify files to snapshot by looking at the current snapshot keys
                # This ensures we don't accidentally snapshot huge binary folders like node_modules
                # unless they were part of the project's tracked files.
                # However, for repo, we should also look for known code files.
                tracked_files = list(current_snapshot.keys())
                
                # Scan for any code files in /app to ensure new files are captured
                res = container.exec_run("find . -maxdepth 3 -not -path '*/.*' -not -path './node_modules/*' -not -path './venv/*' -type f", workdir="/app")
                if res.exit_code == 0:
                    found_files = res.output.decode('utf-8', 'replace').strip().split('\n')
                    for f in found_files:
                        f = f.lstrip('./')
                        if f and f not in tracked_files:
                            tracked_files.append(f)
                
                count = 0
                for file_path in tracked_files:
                    # Remove leading /app/ if present
                    clean_path = file_path.replace('/app/', '').lstrip('/')
                    if clean_path in ['repo', 'port']: continue
                    
                    res = container.exec_run(f"cat {clean_path}", workdir="/app")
                    if res.exit_code == 0:
                        current_snapshot[clean_path] = res.output.decode('utf-8', 'replace')
                        count += 1
                
                db.execute("UPDATE forge_history SET files_snapshot = ? WHERE process_id = ?", (json.dumps(current_snapshot), p_id))
                db.commit()
                return count
            except Exception as e:
                print(f"Auto-snapshot failed for {p_id}: {e}")
                return 0

        if action == "stop":
            if not app_id: return "Error: app_id is required to stop a deployment."
            cursor = db.execute('SELECT process_id, files_snapshot FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Deployment '{app_id}' not found."
            
            p_id = row['process_id']
            current_snapshot = json.loads(row['files_snapshot'])
            
            # Deterministic Auto-Snapshot before destruction
            snap_count = _perform_snapshot(p_id, f"stellar-repo-{p_id}", current_snapshot)
            
            stop_and_cleanup_app_by_process_id(p_id, app_type='forge')
            db.execute("UPDATE forge_history SET status = 'stopped' WHERE process_id = ?", (p_id,))
            db.commit()
            
            return f"Deployment '{app_id}' has been stopped. Auto-snapshotted {snap_count} files for persistence."

        if action == "restart":
            if not app_id: return "Error: app_id is required to restart a deployment."
            
            cursor = db.execute('SELECT process_id, project_name, files_snapshot, subdomain FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Deployment '{app_id}' not found."
            
            process_id = row['process_id']
            project_title = row['project_name']
            subdomain = row['subdomain']
            current_snapshot = json.loads(row['files_snapshot'])
            
            # Deterministic Auto-Snapshot before destruction
            _perform_snapshot(process_id, f"stellar-repo-{process_id}", current_snapshot)
            
            # Reload updated snapshot
            cursor = db.execute('SELECT files_snapshot FROM forge_history WHERE process_id = ?', (process_id,))
            row = cursor.fetchone()
            snapshot = json.loads(row['files_snapshot'])
            
            repo_url = snapshot.get('repo')
            port = snapshot.get('port', 3000)
            
            # Check if it's a Forge project
            is_forge = 'app.py' in snapshot and 'index.html' in snapshot and not repo_url
            
            if is_forge:
                from app import _deploy_and_stream_output
                stop_and_cleanup_app_by_process_id(process_id, app_type='forge')
                app_obj = current_app._get_current_object()
                thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, snapshot, process_id, None, 'forge', subdomain), daemon=True)
                thread.start()
                return f"Forge project '{project_title}' restarted with latest edits! Live URL: https://{subdomain}.stellarai.live/"

            # Generic Repo/Custom restart
            stop_and_cleanup_app_by_process_id(process_id, app_type='forge')
            
            client = docker.from_env()
            from app import ensure_user_network
            user_network = ensure_user_network(client, session['user_id'])
            r_key = _redis_forge_key(process_id)

            container = client.containers.run(
                image='stellar-repo-host:latest',
                command='sleep infinity',
                ports={f"{port}/tcp": ('0.0.0.0', 0)},
                name=f"stellar-repo-{process_id}",
                detach=True,
                init=True,
                working_dir='/app',
                network=user_network,
                labels={
                    "stellar_type": "forge",
                    "stellar_process_id": process_id,
                    "created_at_ts": str(time.time()),
                    "forge_app_id": process_id,
                    "subdomain": subdomain
                }
            )

            time.sleep(2)
            container.reload()
            host_port = container.attrs['NetworkSettings']['Ports'][f"{port}/tcp"][0]['HostPort']

            redis_client.hset(r_key, mapping={"container_id": container.id, "status": "running", "process_id": process_id, "host_port": str(host_port), "files": json.dumps(snapshot)})
            with active_apps_lock: active_apps[process_id] = {"container_id": container.id, "port": host_port, "status": "running"}

            if repo_url:
                container.exec_run(f"git clone {repo_url} .", workdir="/app")
            
            # Restore manual edits from snapshot
            for fname, content in snapshot.items():
                if fname in ['repo', 'port'] or not isinstance(content, str): continue
                b64_content = base64.b64encode(content.encode()).decode()
                container.exec_run(f"python3 -c \"import base64; import os; os.makedirs(os.path.dirname('{fname}'), exist_ok=True); open('{fname}', 'wb').write(base64.b64decode('{b64_content}'))\"", workdir="/app")

            public_url = f"https://{subdomain}.stellarai.live/" if subdomain else f"https://{process_id}.stellarai.live/"
            
            # Re-run health check loop
            status_code = 0
            for _ in range(30):
                time.sleep(1)
                try:
                    res = container.exec_run(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/")
                    if res.exit_code == 0:
                        status_code = int(res.output.decode().strip())
                        if 0 < status_code < 500:
                            break
                        elif status_code >= 500:
                            break # Fail fast on server error
                except: pass
            
            if 0 < status_code < 500:
                ready_msg = f"and server is READY (Status {status_code})!"
            elif status_code >= 500:
                ready_msg = f"but server returned ERROR {status_code}. Check your logs!"
            else:
                ready_msg = "but server is not responding yet. Use repo_control action='execute' to start your app."
                
            return f"Deployment '{project_title}' restarted with latest snapshotted edits {ready_msg} ID: `{process_id}`. Live URL: {public_url}"

        if action == "snapshot":
            if not app_id or not files:
                return "Error: app_id and a list of 'files' paths are required to snapshot manual edits."
            
            cursor = db.execute('SELECT process_id, files_snapshot FROM forge_history WHERE (project_name = ? OR process_id = ? OR subdomain = ?) AND user_id = ? ORDER BY id DESC LIMIT 1', (app_id, app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            if not row: return f"Error: Deployment '{app_id}' not found."
            
            p_id = row['process_id']
            current_snapshot = json.loads(row['files_snapshot'])
            
            client = docker.from_env()
            container_name = f"stellar-repo-{p_id}"
            try:
                container = client.containers.get(container_name)
            except:
                return "Error: Container not found. Cannot snapshot files from a stopped or missing container."
                
            count = 0
            for file_path in files:
                # Remove leading /app/ if present
                clean_path = file_path.replace('/app/', '').lstrip('/')
                res = container.exec_run(f"cat {clean_path}", workdir="/app")
                if res.exit_code == 0:
                    current_snapshot[clean_path] = res.output.decode('utf-8', 'replace')
                    count += 1
            
            db.execute("UPDATE forge_history SET files_snapshot = ? WHERE process_id = ?", (json.dumps(current_snapshot), p_id))
            db.commit()
            return f"Successfully snapshotted {count} files to the database for project '{app_id}'. These edits will now persist across restarts."

        return f"Error: Unknown action '{action}'."
    except Exception as e:
        return f"Error in repo_control: {str(e)}"

# Remove host_repo and repo_execute functions and their available_tools entries



def lab_execute(command: str, status: str, timeout: int = 60) -> str:
    """Executes a bash command in a persistent, isolated Docker sandbox.
    Use this tool to experiment, test Python code, install libraries (apt-get/pip), run shell scripts, clone git repos, or inspect external APIs.
    The sandbox persists across turns, so you can install a tool in one turn and use it in the next.
    
    Args:
        command: The bash command to run (e.g. `python3 script.py`, `pip install x`, `curl -I https...`).
        status: Status update for the user.
        timeout: Execution timeout in seconds (default 60).
    """
    import subprocess
    import docker
    from app import SANDBOX_DIR, UPLOAD_FOLDER
    import os
    import re
    import shutil
    from flask import session
    
    # Attempt to grab current session context dynamically
    try:
        session_id = session.sid
        chat_id = session.get('current_chat_id', 'default')
        user_id = session.get('user_id')
        # Sanitize for Docker container naming
        sanitized_sid = re.sub(r'[^a-zA-Z0-9]', '', str(session_id))
        sanitized_cid = re.sub(r'[^a-zA-Z0-9]', '', str(chat_id))
        
        container_name = f"stellar-lab-{sanitized_sid}-{sanitized_cid}"
        workspace_name = f"lab_workspace_{sanitized_sid}_{sanitized_cid}"
    except:
        # Fallback if no session (though highly unlikely in this app)
        container_name = "stellar-lab-sandbox"
        workspace_name = "lab_workspace"
        user_id = None

    image_name = "stellar-lab-core:latest"
    
    try:
        client = docker.from_env()
        from app import ensure_user_network
        user_network = ensure_user_network(client, user_id)
    except Exception as e:
        return f"Error: Docker client not available: {str(e)}"
        
    # Create a private workspace for the lab
    lab_workspace = os.path.abspath(os.path.join(SANDBOX_DIR, workspace_name))
    os.makedirs(lab_workspace, exist_ok=True)

    # --- DETERMINISTIC FIX: Auto-sync uploaded files to the lab workspace ---
    try:
        local_dir = os.path.join(UPLOAD_FOLDER, str(session.sid))
        if os.path.exists(local_dir):
            for f in os.listdir(local_dir):
                src = os.path.join(local_dir, f)
                dst = os.path.join(lab_workspace, f)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
    except Exception as sync_e:
        pass # Silent fail if permissions or path issues
    # ------------------------------------------------------------------------

    # --- RED TEAM MANDATE INJECTION ---
    mandate_path = os.path.join(lab_workspace, "RED_TEAM_MANDATE.md")
    if not os.path.exists(mandate_path):
        try:
            with open(mandate_path, "w", encoding="utf-8") as f:
                f.write("""# STELLAR RED TEAM MANDATE: IMPACT-DRIVEN OFFENSIVE PROTOCOL (IDOP)

1. **Core Rule**: Discovery alone is insufficient. Every valid finding must be driven to confirmed impact or explicit failure.

2. **Mandatory Exploitation Lifecycle**: Every finding MUST go through: Discovery → Validation → Exploitation → Impact OR Failure. Stopping at discovery is forbidden.

3. **Exploitation Completion Requirement**: If you find credentials, tokens, endpoints, or config leaks, you MUST:
   - Step 1: Validate usability (Are credentials accepted? Does token authenticate?).
   - Step 2: Attempt exploitation (Login, call privileged API, retrieve data).
   - Step 3: Confirm outcome (SUCCESS impact achieved or FAILURE with reason).

4. **No Dead-End Findings**: Do NOT output "JWT found" or ".env exposed" without answering "What can I actually do with this?".

5. **Mandatory Outcome Classification**: Every finding MUST end as: EXPLOITED, BLOCKED, or NON-IMPACTFUL.

6. **Enforced Questions**:
   - Can I use this to authenticate?
   - Can I access restricted data?
   - Can I modify system state?
   - Can I escalate privileges?
   If all NO -> NON-IMPACTFUL.

7. **Exploit Depth Requirement**: Do not stop at surface-level validation. Attempt chaining, alternate payloads, and multiple request variations.

8. **Failure Reporting**: If exploitation fails, state the exact reason (e.g., "JWT valid format but rejected → signature enforced").

9. **No Passive Recon Mode**: Behave as an operator attempting to achieve control, not a scanner or enumerator.

10. **Output Structure**:
[DISCOVERY] What was found
[VALIDATION] Does it work?
[EXPLOIT ATTEMPT] What was tried
[RESULT] SUCCESS / BLOCKED / NON-IMPACTFUL
[REASON] Why it succeeded or failed
""")
        except Exception:
            pass # Failsafe against write permission errors
    # ----------------------------------

    # Ensure sandbox container is running
    container = None
    try:
        container = client.containers.get(container_name)
        if container.status != 'running':
            container.start()
    except docker.errors.NotFound:
        try:
            container = client.containers.run(
                image_name,
                name=container_name,
                detach=True,
                tty=True,
                init=True,
                working_dir='/lab',
                volumes={lab_workspace: {'bind': '/lab', 'mode': 'rw'}},
                restart_policy={"Name": "unless-stopped"},
                network=user_network
            )
        except Exception as e:
            return f"Failed to start Lab sandbox: {str(e)}"
    
    # Execute the command
    try:
        # Wrap command to capture stdout and stderr together and handle errors gracefully
        wrapped_cmd = f"bash -c {subprocess.list2cmdline([command])}"
        
        exec_result = container.exec_run(
            wrapped_cmd,
            demux=False, # Get combined stdout/stderr
            workdir="/lab"
        )
        
        output = exec_result.output.decode('utf-8', 'replace')
        exit_code = exec_result.exit_code
        
        if exit_code != 0:
            return f"Command failed with exit code {exit_code}.\nOutput:\n{output}"
        
        return output if output else "Command executed successfully with no output."
        
    except Exception as e:
        return f"Error executing command in Lab: {str(e)}"

def read_tool_output(output_id: int, status: str, start_line: int = 0, max_lines: int = 100) -> str:
    """Reads a specific slice of a past tool's output from the database.
    Use this when a tool's history says [Output truncated] to retrieve the full text without polluting your context window.
    Args:
        output_id: The ID of the tool execution to read.
        status: Status update for the user.
        start_line: The line number to start reading from (0-indexed). Default is 0.
        max_lines: The maximum number of lines to return. Default is 100.
    """
    import sqlite3
    try:
        from app import DATABASE_NAME
        conn = sqlite3.connect(DATABASE_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('SELECT result FROM tool_calls WHERE id = ?', (output_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return f"Error: No tool output found with ID {output_id}."

        res_str = str(row['result'])
        lines = res_str.split('\n')
        total_lines = len(lines)

        if start_line >= total_lines:
            return f"Error: start_line {start_line} is beyond the total lines ({total_lines})."

        end_line = min(start_line + max_lines, total_lines)
        sliced_lines = lines[start_line:end_line]

        output = f"--- Tool Output ID: {output_id} (Lines {start_line} to {end_line-1} of {total_lines}) ---\n"
        output += '\n'.join(sliced_lines)
        if end_line < total_lines:
            output += f"\n--- (Output truncated. {total_lines - end_line} lines remaining. Read from line {end_line} to see more) ---"
        
        return output
    except Exception as e:
        return f"Error reading tool output: {str(e)}"

def analyze_youtube_video(query: str, status: str, action: str = "analyze", video_url: str = None, start_time: str = None, end_time: str = None, fps: int = 1, max_results: int = 5, model_id: str = "gemini-3.1-flash-lite-preview") -> str:
    """Analyzes a specific YouTube video or searches for the best video based on a query.
    Args:
        query: What you want to find/analyze. Mandatory for both actions.
        status: Status update for the user.
        action: 'analyze' (default) to interrogate a video's content, or 'search' to find videos matching the query.
        video_url: The full YouTube URL (required for 'analyze').
        start_time: Optional start offset for 'analyze' (e.g., '1m10s', '60s').
        end_time: Optional end offset for 'analyze' (e.g., '2m30s', '120s').
        fps: Frames per second to sample from the video for 'analyze' (default 1).
        max_results: Number of search results to return (default 5, max 50).
        model_id: (Internal) The model to use for analysis.
    """
    from app import PRIMARY_API_KEY, YOUTUBE_API_KEY
    
    if action == "search":
        if not YOUTUBE_API_KEY:
            return "Error: YouTube API key is not configured in the backend."
        try:
            # Enforce max 50 limit
            limit = min(int(max_results), 50)
            
            # Step 1: Search for Video IDs
            search_url = "https://www.googleapis.com/youtube/v3/search"
            search_params = {
                "part": "snippet",
                "q": query,
                "maxResults": limit,
                "type": "video",
                "key": YOUTUBE_API_KEY
            }
            search_response = requests.get(search_url, params=search_params).json()
            if "error" in search_response:
                return f"YouTube Search API Error: {json.dumps(search_response['error'])}"
            
            items = search_response.get("items", [])
            video_ids = [item["id"]["videoId"] for item in items]
            if not video_ids:
                return "No YouTube videos found for the given query."

            # Step 2: Get detailed stats (Views, Likes, Duration, and Full Description)
            stats_url = "https://www.googleapis.com/youtube/v3/videos"
            stats_params = {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY
            }
            stats_response = requests.get(stats_url, params=stats_params).json()
            
            enriched_results = []
            for stat_item in stats_response.get("items", []):
                v_id = stat_item["id"]
                snippet = stat_item.get("snippet", {})
                
                enriched_results.append({
                    "title": snippet.get("title"),
                    "channelTitle": snippet.get("channelTitle"),
                    "description": snippet.get("description"),
                    "video_id": v_id,
                    "viewCount": int(stat_item.get("statistics", {}).get("viewCount", 0)),
                    "likeCount": int(stat_item.get("statistics", {}).get("likeCount", 0)),
                    "duration": stat_item.get("contentDetails", {}).get("duration", "N/A"),
                    "url": f"https://www.youtube.com/watch?v={v_id}"
                })
            
            # Sort by viewCount descending
            enriched_results.sort(key=lambda x: x["viewCount"], reverse=True)
            return json.dumps(enriched_results, indent=2)
        except Exception as e:
            return f"Error during YouTube search: {str(e)}"

    # Default 'analyze' logic
    if not video_url:
        return "Error: 'video_url' is required for the 'analyze' action."

    client = genai.Client(api_key=PRIMARY_API_KEY, http_options={'api_version': 'v1beta'})
    
    part = types.Part(
        file_data=types.FileData(
            file_uri=video_url,
            mime_type="video/*",
        )
    )
    
    video_metadata = {}
    if start_time: video_metadata['start_offset'] = start_time
    if end_time: video_metadata['end_offset'] = end_time
    if fps: video_metadata['fps'] = int(fps)
    
    if video_metadata:
        part.video_metadata = types.VideoMetadata(**video_metadata)
        
    contents = [
        types.Content(
            role="user",
            parts=[part, types.Part.from_text(text=query)]
        )
    ]
    
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=contents
        )
        return response.text if response.text else "The model returned an empty response for the video analysis."
    except Exception as e:
        return f"Error analyzing YouTube video: {str(e)}"

def manage_files(action: str, status: str, file_name: str = None, target_env: str = "lab", source_env: str = "chat") -> str:
    """
    Manage user-uploaded files or export code out of execution environments.
    Args:
        action: 'read' (list uploads), 'move' (transfer file/folder), 'project' (export to user).
        status: Status update for the user.
        file_name: The name of the file or folder to move or project.
        target_env: 'lab' or process_id for repo.
        source_env: 'chat' (uploads), 'lab', or process_id for repo.
    """
    from flask import session
    from app import app, UPLOAD_FOLDER, get_db
    import os
    import docker
    import base64
    import uuid
    import tarfile
    import io
    import re

    try:
        client = docker.from_env()
    except Exception as e:
        return f"Error: Docker client not available: {str(e)}"

    try:
        session_id = str(session.sid)
        chat_id = str(session.get('current_chat_id', 'default'))
        sanitized_sid = re.sub(r'[^a-zA-Z0-9]', '', session_id)
        sanitized_cid = re.sub(r'[^a-zA-Z0-9]', '', chat_id)
        dynamic_lab_container = f"stellar-lab-{sanitized_sid}-{sanitized_cid}"
    except:
        return "Error: Active session context required."

    def validate_env_ownership(env_id):
        if env_id in ("lab", "chat"): return True
        try:
            db = get_db()
            cursor = db.execute('SELECT 1 FROM forge_history WHERE process_id = ? AND user_id = ?', (env_id, session['user_id']))
            if cursor.fetchone(): return True
            if env_id == session.get('forge_project', {}).get('process_id'): return True
            if env_id == session.get('last_run_code_process_id'): return True
        except: pass
        return False

    if not validate_env_ownership(target_env):
        return f"Error: Unauthorized access to target environment '{target_env}'."
    if not validate_env_ownership(source_env):
        return f"Error: Unauthorized access to source environment '{source_env}'."

    def get_container_name(env_id):
        if env_id == "lab": return dynamic_lab_container
        # Try repo prefix first
        repo_name = f"stellar-repo-{env_id}"
        try:
            client.containers.get(repo_name)
            return repo_name
        except:
            # Fallback to forge prefix
            return f"stellar-forge-{env_id}"

    local_uploads = os.path.join(UPLOAD_FOLDER, session_id)

    if action == "read":
        if not os.path.exists(local_uploads): return "No uploads found."
        files = os.listdir(local_uploads)
        return "Files in chat context:\n" + "\n".join([f"- {f}" for f in files]) if files else "No uploads found."

    elif action == "move":
        if not file_name: return "Error: 'file_name' required."
        
        # Determine source and target containers/paths
        target_name = get_container_name(target_env)
        target_dir = "/lab" if target_env == "lab" else "/app"
        
        try:
            target_cont = client.containers.get(target_name)
            
            # --- SOURCE: CHAT (LOCAL UPLOADS) ---
            if source_env == "chat":
                src_path = os.path.join(local_uploads, file_name)
                if not os.path.exists(src_path): return f"File '{file_name}' not found in uploads."
                
                # Security: Restrict host moves to only UPLOAD_FOLDER and OUTPUTS
                allowed_prefixes = [os.path.abspath(UPLOAD_FOLDER), os.path.abspath("outputs")]
                if not any(os.path.abspath(src_path).startswith(p) for p in allowed_prefixes):
                    return "Security Error: Host move restricted to specific directories."

                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode='w') as tar:
                    tar.add(src_path, arcname=os.path.basename(src_path))
                tar_stream.seek(0)
                if target_cont.put_archive(target_dir, tar_stream):
                    return f"Moved '{file_name}' from chat to {target_env}."
            
            # --- SOURCE: ANOTHER CONTAINER ---
            else:
                source_name = get_container_name(source_env)
                source_dir = "/lab" if source_env == "lab" else "/app"
                src_cont = client.containers.get(source_name)
                
                # Fetch as tar stream from source
                bits, stat = src_cont.get_archive(f"{source_dir}/{file_name}")
                tar_data = io.BytesIO()
                for chunk in bits: tar_data.write(chunk)
                tar_data.seek(0)
                
                if target_cont.put_archive(target_dir, tar_data):
                    return f"Moved '{file_name}' from {source_env} to {target_env}."

        except Exception as e:
            return f"Move failed: {str(e)}"

    elif action == "project":
        if not file_name: return "Error: 'file_name' required."
        source_name = get_container_name(target_env) # target_env is used as source here
        source_dir = "/lab" if target_env == "lab" else "/app"
        
        try:
            container = client.containers.get(source_name)
            
            # Check if it is a directory
            check_dir = container.exec_run(f"test -d {source_dir}/{file_name}")
            is_dir = (check_dir.exit_code == 0)
            
            final_file_name = file_name
            if is_dir:
                # Zip it in the container first
                zip_name = f"{file_name}_export.tar.gz"
                container.exec_run(f"tar -czf /tmp/{zip_name} -C {source_dir}/{file_name} .")
                src_full_path = f"/tmp/{zip_name}"
                final_file_name = zip_name
            else:
                src_full_path = f"{source_dir}/{file_name}"

            # Get archive from container
            bits, stat = container.get_archive(src_full_path)
            tar_data = io.BytesIO()
            for chunk in bits: tar_data.write(chunk)
            tar_data.seek(0)
            
            # Extract from tar to get raw bytes
            with tarfile.open(fileobj=tar_data) as tar:
                member = tar.getmembers()[0]
                file_bytes = tar.extractfile(member).read()

            output_dir = "outputs"
            os.makedirs(output_dir, exist_ok=True)
            unique_name = f"proj_{uuid.uuid4().hex[:6]}_{final_file_name}"
            with open(os.path.join(output_dir, unique_name), "wb") as f:
                f.write(file_bytes)
                
            return f"Projected successfully: [Download {final_file_name}](/download/{unique_name})"
            
        except Exception as e:
            return f"Projection failed: {str(e)}"
            
    return "Error: Invalid action."
            
    return "Error: Invalid action. Use 'read', 'move', or 'project'."

# Define the tools list for Gemini

available_tools = [
    native_search,
    extensive_search,
    generate_image,
    make_presentation,
    regenerate_presentation_slide,
    analyze_youtube_video,
    manage_files,
    forge_control,
    repo_control,
    lab_execute,
    read_tool_output,
    logs_and_preferences
]

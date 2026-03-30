import time
import uuid
import requests
import json
import base64
import os
from tavily import TavilyClient
import asyncio
from google import genai
from google.genai import types

import logging
logger = logging.getLogger(__name__)

def extensive_search(query: str, topic: str = "general", days: int = 3, max_results: int = 10) -> str:
    """Performs a deep web search using Tavily API.
    Args:
        query: The search query
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

def generate_image(model: str, prompt: str, quality: str = "standard", aspect_ratio: str = "1:1") -> str:
    """Generates an image using Gemini's Imagen model.
    Args:
        model: 'gemini-3.1-flash-image-preview' or 'gemini-3-pro-image-preview'
        prompt: detailed descriptive prompt for the image
        quality: 'standard' or 'hd'
        aspect_ratio: '1:1', '16:9', '4:3', etc.
    """
    from app import PRIMARY_API_KEY
    client = genai.Client(api_key=PRIMARY_API_KEY)
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                # Specific config for Imagen if needed, else standard
            )
        )
        # Look for the image in the response parts
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                img_data = part.inline_data.data
                img_b64 = base64.b64encode(img_data).decode('utf-8')
                return f"![Generated Image](data:image/png;base64,{img_b64})"
        
        return "No image data found in response."
    except Exception as e:
        return f"Error generating image: {str(e)}"

def native_search(prompt: str) -> str:
    """Uses gemini-2.5-flash-lite with Google Search tool enabled to search the web and return the result.
    Args:
        prompt: A fully self-contained search query to send to Google. Never use pronouns like 'it' or 'that', specify exactly what you are looking for.
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

def render_svg(instructions: str, model_id: str = 'gemini-3.1-pro-preview') -> str:
    """Generate and render an SVG visual experience directly inside the chat bubble.
    The function returns a complete, self-contained SVG string which will be rendered inline inside the chat bubble.
    Args:
        instructions: natural language description of the visual to create
        model_id: The model to use for SVG generation (internal use)
    """
    from app import PRIMARY_API_KEY
    from pydantic import BaseModel, Field
    import json
    
    class SVGResponse(BaseModel):
        svg_code: str = Field(description="The raw, valid, self-contained SVG code. Must not include markdown wrappers.")

    try:
        # Explicit log to verify tool chaining and dynamic model selection
        logger.info(f"[TOOL] render_svg invoked with model_id: {model_id}")
        
        client = genai.Client(api_key=PRIMARY_API_KEY)
        sys_instruct = (
            "You are an expert SVG designer. You must return ONLY valid, self-contained, and interactive/animated SVG code "
            "based on the user's instructions. "
            "STRICT RULES:\n"
            "1. DO NOT include any markdown wrappers (no ```svg, no ```xml, etc.).\n"
            "2. DO NOT include a solid background rectangle (e.g. <rect width='100%' height='100%' fill='#...'>). The background MUST remain transparent to blend into the UI.\n"
            "3. Ensure all shapes, text, and animations are clearly visible against both light and dark backgrounds (use high contrast or glow effects).\n"
            "4. Use <animate>, <animateTransform>, or <animateMotion> for professional animations.\n"
            "5. The output must be a single, complete <svg> element."
        )
        response = client.models.generate_content(
            model=model_id,
            contents=instructions,
            config=types.GenerateContentConfig(
                system_instruction=sys_instruct,
                response_mime_type="application/json",
                response_schema=SVGResponse,
                temperature=0.2
            )
        )
        data = json.loads(response.text)
        svg_code = data.get("svg_code", "").strip()
        # Robustly strip markdown wrappers just in case the model ignored sys_instruct
        import re
        svg_code = re.sub(r'^```(?:svg|xml)?\s*', '', svg_code, flags=re.IGNORECASE)
        svg_code = re.sub(r'\s*```$', '', svg_code)
        return svg_code.strip()
    except Exception as e:
        return f"<svg width='200' height='50'><text x='10' y='30' fill='red'>Error: {str(e)}</text></svg>"

def make_presentation(topic: str, num_slides: int = 10, style: str = "corporate", additional_context: str = "") -> str:
    """Generate a fully designed PowerPoint presentation where each slide is a full-bleed AI generated image containing text.
    Args:
        topic: The topic of the presentation
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

def regenerate_presentation_slide(presentation_id: str, slide_index: int, topic: str, style: str, additional_context: str = "", feedback: str = "") -> str:
    """Regenerate a specific slide of an existing presentation based on feedback.
    Args:
        presentation_id: the ID of the presentation
        slide_index: 0-based index of the slide to regenerate
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
    
    # Re-plan only this slide
    slide_plan_prompt = (
        f"Re-plan slide index {slide_index} for a presentation on '{topic}'.\n"
        f"Original Style: {style}.\n"
        f"Context: {additional_context}.\n"
        f"USER FEEDBACK FOR REGENERATION: {feedback}.\n"
        "Design it as a complete visual experience. Include specific sections, diagrams, or icons in the background description."
    )

    class Slide(BaseModel):
        title: str = Field(description="Main title for the slide.")
        summary: str = Field(description="A comprehensive, detailed summary for the slide.")
        background_description: str = Field(description="Detailed description of the visual layout.")

    try:
        resp = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=slide_plan_prompt,
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
        f"A professional, high-resolution 16:9 presentation infographic slide. Style: '{style}'.\n"
        f"LAYOUT: {slide_data.get('background_description')}.\n"
        f"CONTENT TO RENDER DIRECTLY IN IMAGE:\n"
        f"Main Header: '{slide_data.get('title')}'\n"
        f"Body Text: '{slide_data.get('summary')}'\n"
        f"ADDITIONAL FEEDBACK: {feedback}\n"
        "INSTRUCTIONS:\n"
        "- Use professional, clean sans-serif typography.\n"
        "- Integrate the text aesthetically into a multi-column or structured infographic layout.\n"
        "- Include relevant icons, charts, or transition diagrams if mentioned in the layout description.\n"
        "- The result must be a single, complete, polished slide design. No watermarks. Legible text."
    )

    try:
        result = client.models.generate_content(
            model='gemini-3.1-flash-image-preview',
            contents=full_image_prompt,
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


def forge_control(action: str, app_id: str = None, changes: dict = None, prompt: str = None, project_name: str = None) -> str:
    """Control the user's Forge deployments.
    Args:
        action: "list_history", "create", or "modify"
        app_id: the Forge application identifier or Project Title (required for 'modify')
        changes: key-value config/code changes to apply (e.g. {'app.py': '...'})
        prompt: Instruction for AI-driven modification or creation
        project_name: Optional name for a new project (if omitted, one is generated)
    Returns:
        deployment status, history list, or live URL
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
            cursor = db.execute('SELECT project_name, process_id, status, created_at FROM forge_history WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],))
            history = cursor.fetchall()
            if not history:
                return "You have no past Forge deployments."
            
            res = "### Your Forge Deployment History:\n"
            for row in history:
                url_str = f" - [Visit App](https://stellarai.live/apps/{row['process_id']}/)" if row['status'] == 'running' else ""
                res += f"- **{row['project_name']}** (ID: `{row['process_id']}`) - Status: {row['status']} - Created: {row['created_at']}{url_str}\n"
            return res

        actual_app_id = None
        project_title = None
        current_files = {}
        old_container_id = None
        db = get_db()

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
            
            session['forge_project'] = {'files': current_files, 'container_id': None, 'process_id': actual_app_id, 'project_name': project_title}
            session.modified = True
            
            db.execute('INSERT INTO forge_history (user_id, project_name, process_id, status, files_snapshot) VALUES (?, ?, ?, ?, ?)',
                       (session['user_id'], project_title, actual_app_id, 'starting', json.dumps(current_files)))
            db.commit()

        elif action == "modify":
            if not app_id:
                return "Error: app_id or Project Title is required for modification."
            
            # Resolve title/ID with fuzzy matching
            cursor = db.execute('SELECT process_id, project_name, files_snapshot FROM forge_history WHERE (project_name = ? OR process_id = ?) AND user_id = ?', (app_id, app_id, session.get('user_id')))
            row = cursor.fetchone()
            
            if not row:
                fuzzy_query = f"%{app_id}%"
                cursor = db.execute('SELECT process_id, project_name, files_snapshot FROM forge_history WHERE project_name LIKE ? AND user_id = ? ORDER BY created_at DESC', (fuzzy_query, session.get('user_id')))
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
                
            session['forge_project'] = {'files': current_files, 'container_id': None, 'process_id': actual_app_id, 'project_name': project_title}
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
        thread = threading.Thread(target=_deploy_and_stream_output, args=(app_obj, current_files, actual_app_id, old_container_id, 'forge'), daemon=True)
        thread.start()

        # Shared Wait Loop
        start_wait = time.time()
        final_status = "starting"
        public_url = f"https://stellarai.live/apps/{actual_app_id}/"
        
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

# Define the tools list for Gemini
available_tools = [
    native_search,
    extensive_search,
    generate_image,
    render_svg,
    make_presentation,
    regenerate_presentation_slide,
    forge_control
]

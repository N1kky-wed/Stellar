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

def extensive_search(query: str, search_depth: str = "basic", topic: str = "general", days: int = 3, max_results: int = 5, include_domains: list[str] = None, exclude_domains: list[str] = None, include_answer: bool = False, include_raw_content: bool = False, include_images: bool = False, include_image_descriptions: bool = False) -> str:
    """Performs an extensive web search using the Tavily API.
    Args:
        query: search query
        search_depth: "basic" or "advanced"
        topic: "general" or "news"
        days: number of days back for news topic
        max_results: max number of results
        include_domains: list of domains to include
        exclude_domains: list of domains to exclude
        include_answer: include a short answer
        include_raw_content: include raw HTML content
        include_images: include image URLs
        include_image_descriptions: include image descriptions
    """
    from app import TAVILY_API_KEY
    if not TAVILY_API_KEY:
        return json.dumps({"error": "Tavily API key not found."})
    
    client = TavilyClient(api_key=TAVILY_API_KEY)
    try:
        response = client.search(
            query=query,
            search_depth=search_depth,
            topic=topic,
            days=days,
            max_results=max_results,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            include_answer=include_answer,
            include_raw_content=include_raw_content,
            include_images=include_images,
            include_image_descriptions=include_image_descriptions
        )
        return json.dumps(response)
    except Exception as e:
        return json.dumps({"error": str(e)})


def generate_image(model: str, prompt: str, quality: str = "standard", aspect_ratio: str = "1:1") -> str:
    """Generates an image using Gemini API.
    Args:
        model: one of gemini-3.1-flash-image-preview or gemini-3-pro-image-preview
        prompt: description of the image to generate
        quality: e.g. "standard" or "hd"
        aspect_ratio: e.g. "1:1", "16:9", "9:16", "4:3"
    """
    from app import PRIMARY_API_KEY
    try:
        client = genai.Client(api_key=PRIMARY_API_KEY)
        
        result = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        
        if not result.candidates or not result.candidates[0].content.parts:
            return "No image generated."

        for part in result.candidates[0].content.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                img_bytes = part.inline_data.data
                mime_type = part.inline_data.mime_type or "image/jpeg"
                img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                return f"![Generated Image](data:{mime_type};base64,{img_b64})"
        
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
            "Ensure the SVG uses <animate>, <animateTransform>, or embedded <script> tags when appropriate, "
            "and that the background is transparent (or fits the design) so it blends in."
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
        return data.get("svg_code", "").strip()
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
            # Find the slide to replace. python-pptx doesn't have an easy "replace image", 
            # so we might have to add a new slide and move it, or just re-save the whole thing.
            # For now, let's just update the image on that slide if we can find it.
            if slide_index < len(prs.slides):
                slide = prs.slides[slide_index]
                # Remove old shapes?
                for shape in list(slide.shapes):
                    # Keep it simple: remove all and add new picture
                    sp = shape._element
                    sp.getparent().remove(sp)
                
                image_stream = BytesIO(img_bytes)
                slide.shapes.add_picture(image_stream, 0, 0, width=prs.slide_width, height=prs.slide_height)
                prs.save(pptx_filepath)
        except Exception as e:
            print(f"Error updating PPTX: {e}")

    return f"REGENERATED_SLIDE:{json.dumps({'presentation_id': presentation_id, 'slide_index': slide_index, 'url': f'/view/pres_{presentation_id}/{slide_img_filename}'})}"


def forge_control(action: str, app_id: str, changes: dict = None) -> str:
    """Control the user's Forge deployments.
    Args:
        action: "redeploy" or "modify"
        app_id: the Forge application identifier
        changes: key-value config/code changes to apply (e.g. {'app.py': '...'})
    Returns:
        deployment status + live URL of the application
    """
    from app import db, run_forge_deployment, get_current_session_id
    from flask import session
    
    try:
        import docker
        client = docker.from_env()
        containers = client.containers.list(filters={"label": f"forge_app_id={app_id}"})
        if not containers:
            containers = client.containers.list(filters={"label": f"stellar_process_id={app_id}"})
        
        if not containers:
            return f"Error: App {app_id} is stopped or not found. It must be running to modify."
            
        container = containers[0]
        
        if action == "modify" and changes:
            temp_dir_path = None
            for mount in container.attrs.get('Mounts', []):
                if mount['Destination'] == '/app':
                    temp_dir_path = mount['Source']
                    break
            
            if temp_dir_path:
                for file_path, content in changes.items():
                    full_path = os.path.join(temp_dir_path, file_path)
                    with open(full_path, "w", encoding="utf-8") as f:
                        f.write(content)
            else:
                return "Error: Could not find sandbox directory for the app."
                
        container.exec_run("pkill -f 'python app.py'")
        container.exec_run(["sh", "-c", "python app.py > app.log 2>&1"], detach=True)
        
        return f"Deployment successful. App {app_id} modified/restarted. Live URL: /apps/{app_id}/"
    except Exception as e:
        return f"Error controlling forge: {str(e)}"

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

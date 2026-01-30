import os
import queue
import time
import dotenv
import itertools
import logging
import shutil
import werkzeug.utils
from PyPDF2 import PdfReader, PdfWriter
import io
from google import generativeai as genai
from google.api_core import exceptions as google_exceptions
from google.generativeai.types import generation_types
import uuid

dotenv.load_dotenv()

MODEL_NAME = "gemini-2.0-flash-lite"
RATE_LIMIT_DELAY_UPLOAD = 1
RATE_LIMIT_DELAY_GENERATE = 2
MAX_UPLOAD_RETRIES = 2
FILE_PROCESSING_WAIT_TIMEOUT_SECONDS = 300
FILE_PROCESSING_POLL_INTERVAL_SECONDS = 10

PDF_CHUNK_SIZE_LIMIT_MB = 19.5
PDF_CHUNK_PAGE_LIMIT = 10
TEXT_CHUNK_CHAR_LIMIT = 900000

FILE_ANALYSIS_PROMPT = """
**Overall Objective:** Your analysis must serve two purposes:
1.  **Targeted Analysis:** In this section, provide a comprehensive answer to the user's query.
    *   **Answer Directly but Thoroughly:** Begin with a direct answer, then elaborate with full background context from the file.
    *   **Address All Possibilities:** If the query has multiple potential answers and all possible answers of the follow up questions the user might ask, interpretations, or relevant data points, present and explain all of them.
    *   **Enrich the Answer:** Include any directly related details, implications, or nuances found within the document that help provide a complete and insightful understanding.
2.  **Exhaustive General Analysis:** After addressing the user's query, perform a full forensic analysis of the *entire file* to uncover all possible details and anticipate future questions.

**Output Structure:**
1.  **Confirmation of User Query:** Begin by stating the user's query you are working on: '{user_query}'
2.  **Targeted Analysis:** A dedicated section that directly and thoroughly answers the user's query based on the file.
3.  **Comprehensive General Analysis:** Following the targeted analysis, proceed with the full forensic breakdown as outlined in the sections below.
---

**Comprehensive General Analysis Details:**

We require an extreme, forensic-level, pixel-to-pixel analysis of the image content, exhaustive color pattern detection, comprehensive OCR extraction, precise location identification of any scenery or object, and a full-scale, deeply detailed answer for every aspect.

Do not say 'Okay, let's perform an extreme, forensic-level analysis of the provided image and OCR output.' Just directly get to it.
*   **Key Information & Entities **
    *   Identify and meticulously list the most critical, specific, and nuanced pieces of information, leaving no detail overlooked.
    *   **Named Entity Recognition (NER):** Pinpoint and categorize *all* named entities with extreme granularity:
        *   **PERSONS:** Full names, titles, aliases, relationships (if inferable).
        *   **ORGANIZATIONS:** Companies, institutions, government bodies, brands, departments.
        *   **LOCATIONS (Geospatial Precision - Highest Possible Accuracy):** Identify any recognizable landmarks, natural formations, or man-made structures that can pinpoint a precise location. Infer geographical regions, specific addresses (street, city, state, postal code, country), precise latitude/longitude coordinates (if detectable or inferable from context/EXIF data), geographical regions, landmarks, points of interest. Analyze any spatial relationships, directions, or implied movements.
        *   **DATES & TIMES (Temporal Granularity - Micro-Analysis):** Absolute dates (YYYY-MM-DD), relative dates (yesterday, next week), specific timestamps (HH:MM:SS with timezone, milliseconds if present), time ranges, recurring events, inferred time of day or season. Analyze temporal sequences, dependencies, and duration.
        *   **NUMERICAL VALUES (Statistical Significance - All Data Points):** Extract all numerical data points (monetary values with currency codes, percentages, quantities, measurements with units, IDs, serial numbers, codes, dimensions). Specify units where applicable, and note any implied numerical relationships.
        *   **CONTACT INFORMATION:** Phone numbers (with country codes, extensions), email addresses, URLs, social media handles, physical addresses explicitly stated.
        *   **PRODUCT/SERVICE IDENTIFIERS:** SKUs, model numbers, serial numbers, product names, service names, version numbers, patents.
        *   **TOPICS/KEYWORDS:** Extract all significant keywords, phrases, and semantic concepts that define the file's content, prioritizing specificity and relevance.
        *   **EXTERNAL REFERENCES:** Any hyperlinks, file paths, QR codes, barcodes, or other external data links.
    *   **Raw OCR Text Output:**
        [BEGIN OCR TEXT]
        (Perform OCR here and output the complete, raw, unedited text from the image directly in this spot.)
        [END OCR TEXT]

*   **Analysis of Extracted Text (After Performing OCR):**
    *   **Formatting and Layout:** After providing the raw text above, describe the document's structure. Analyze layout detection (columns, paragraphs, lists), font analysis (font families, sizes, weights, styles, colors), text density, character error rates (if obvious), and the logical flow of text blocks. Identify any text-based data visualizations or tables.
    *   **Language and Semantics:** Analyze the language, tone, and potential meaning of the text within the context of the overall file.

*   **Patterns & Relationships (Deep Contextual Analysis):**
    *   Describe all observable trends, correlations, distributions, and recurring patterns within the data, emphasizing their statistical significance and contextual implications. How do different data points or sections interrelate and influence each other?
    *   **Statistical Patterns:**
        *   Comprehensive frequency distributions for categorical data.
        *   Mean, median, mode, standard deviation, variance, quartiles, and range for numerical data.
        *   Advanced correlation coefficients (Pearson, Spearman) between relevant numerical fields.
        *   Rigorous identification of outliers or anomalies, with a detailed explanation of their deviation and potential causes.
    *   **Temporal Patterns:** Seasonal trends, periodicities, growth/decline rates over time, event sequences, and temporal dependencies.
    *   **Geospatial Patterns:** Concentrations of events/entities in specific locations, spatial clustering, proximity relationships, movement patterns (if sequential location data exists), and geographical spread analysis.
    *   **Network Analysis (If Applicable):** Describe intricate relationships between entities (e.g., A refers to B, C communicates with D, X is related to Y). Identify central nodes, communities, hierarchical structures, or influential connections.
    *   **Visual/Auditory Patterns (If Applicable):**
        *   **Exhaustive Color Analysis & Visualization:** Identify *all distinct colors* present in the image, providing their precise hex codes, RGB values, and CMYK values. Detail the dominant color schemes, average colors across different segments, color gradients, and the psychological impact of observed colors. Quantify the frequency of specific colors and provide a detailed *color distribution analysis across the entire image and its discernible segments*. Present this distribution in a way that can be visually interpreted, suggesting appropriate graphical representations (e.g., color palette with percentages, histogram of hue distribution).
        *   **Object/Scene Recognition & Semantic Understanding:** Precisely detect and classify all objects, scene types, environmental elements, and their interrelationships. Perform facial recognition (if applicable and appropriate, noting any detected individuals). Analyze the sentiment, mood, or emotional tone conveyed by visual elements and the overall scene.
        *   **Data Visualization & Graph Recognition (Detailed Characterization):** Identify and meticulously characterize *all types of graphs, charts, or data visualizations* present within the image. For each detected visualization, describe:
            *   Its precise type (e.g., bar chart, line graph, scatter plot, pie chart, area chart, histogram, network graph, treemap, heatmap).
            *   The specific data it represents (variables, categories, values).
            *   Its axes labels, legends, titles, and data points (if legible).
            *   Any discernible trends, patterns, correlations, or anomalies depicted within the visualization itself.
            *   The scale and units used.
        *   **Audio Analysis (If Applicable):** Detected speech, background noise, identified sounds, sentiment in speech, and sound event classification.

*   **Themes & Topics (Granular Semantic Mapping):**
    *   Summarize the overarching main subjects and *all granular sub-themes* represented by the data in the entire file, indicating their prominence and interconnectedness.
    *   **Granular Topic Modeling:** Identify distinct topics, their associated keywords/entities, and quantitatively assess their prominence and distribution throughout the file.
    *   **Sentiment Analysis (If textual/visual):** Determine the overall emotional tone (positive, negative, neutral, mixed, specific emotions) and identify specific sections or entities exhibiting particular sentiment, explaining the basis for this determination.
    *   **Categorization/Classification:** Suggest potential categories or classifications for the data based on its content, providing a rationale for each suggestion.

    Please provide exhaustive descripition on how to acutally recreate that website/Resume in the image if there is any, like what are the headers, Bold/strong/Italic fonts, the side bars, the ui interface etc etc..

*   **Potential Insights & Observations (Strategic & Predictive):**
    *   What critical, unexpected, and actionable conclusions, significant anomalies, profound questions, or groundbreaking insights arise from this file's analysis?
    *   Highlight anything noteworthy, surprising, counter-intuitive, or that significantly challenges preconceived notions.
    *   Propose detailed hypotheses that could be rigorously tested using this data or supplementary information, outlining potential methodologies.
    *   Discuss potential predictive capabilities, strategic implications, risk factors, or opportunities derived from the data analysis.
    *   Identify precise areas where further investigation, external data integration, or cross-referencing with other datasets would be highly beneficial, specifying the type of data needed.
    *   **Anticipated Follow-up Questions:** Based on the user's initial query and your analysis, formulate and answer 3-5 likely follow-up questions. For each question, provide a direct answer supported by evidence from the file. This demonstrates foresight and provides a more complete service.

*   **Data Quality Notes (Meticulous Assessment):**
    *   Provide a meticulous, pixel-level comment on any apparent inconsistencies, missing values, duplicate entries, formatting irregularities, data entry errors, potential biases, or inherent limitations observed in the file data.
    *   Address potential biases in the data collection, representation, or even the image capture process itself.
    *   Suggest concrete, actionable steps for data cleaning, preprocessing, augmentation, or validation to significantly enhance its utility, accuracy, and reliability.
    *   Comment comprehensively on the file's overall completeness, integrity, and the confidence level in the extracted information.
"""

FIRST_CHUNK_ANALYSIS_PROMPT = """
**Analyze the data in the provided file chunk ('{filename}', Chunk 1/{total_chunks}) IN DETAIL using Markdown.**

**This is the FIRST chunk (1/{total_chunks}) of the file '{original_filename}'.**

**Contextual User Query:** '{user_query}'

**Instructions:**
*   Keep the user's query in mind to guide your analysis of this chunk.
*   Focus your analysis *only* on the content within *this first chunk*.
*   Identify key information, entities, patterns, themes, etc., *within this chunk* that are relevant to the user's query.
*   **DO NOT summarize the entire original file yet.**
Conclude with a brief summary *of this chunk's analysis* which will serve as context for the *next* chunk.
"""

CHUNK_ANALYSIS_PROMPT = """
**Analyze the data in the provided file chunk ('{filename}', Chunk {chunk_num}/{total_chunks}) IN DETAIL using Markdown.**

**This is Chunk {chunk_num} of {total_chunks} for the file '{original_filename}'.**

**Contextual User Query:** '{user_query}'

**Context from Previous Chunk Analysis (Result of Chunk {prev_chunk_num}):**
{previous_analysis_summary}

**Instructions:**
*   Keep the user's query in mind to guide your analysis of this chunk.
*   Focus your analysis *only* on the content within *this specific chunk* ({chunk_num}/{total_chunks}).
*   Identify key information, entities, patterns, themes, etc., *within this chunk* that are relevant to the user's query and the previous context.
*   Relate findings to the provided context from the previous chunk if applicable.
*   **DO NOT summarize the entire original file.** Your goal is to analyze this chunk based on its content and the summary of the preceding chunk.
*   Conclude with a brief summary *of this chunk's analysis* which will serve as context for the *next* chunk.
"""

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

def get_mime_type(filename):
    file_ext = os.path.splitext(filename)[1].lower()
    if file_ext == '.pdf': return 'application/pdf'
    if file_ext == '.csv': return 'text/csv'
    if file_ext == '.pptx': return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
    if file_ext in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml', '.log', '.java', '.c', '.cpp', '.h', '.rb', '.php', '.go', '.rs', '.swift', '.kt']: return 'text/plain'
    if file_ext in ['.jpg', '.jpeg']: return 'image/jpeg'
    if file_ext == '.png': return 'image/png'
    if file_ext == '.webp': return 'image/webp'
    if file_ext == '.heic': return 'image/heic'
    if file_ext == '.heif': return 'image/heif'
    if file_ext == '.mp4': return 'video/mp4'
    if file_ext == '.mov': return 'video/quicktime'
    if file_ext == '.avi': return 'video/x-msvideo'
    if file_ext == '.mpeg': return 'video/mpeg'
    if file_ext == '.webm': return 'video/webm'
    if file_ext == '.mp3': return 'audio/mpeg'
    if file_ext == '.wav': return 'audio/wav'
    if file_ext == '.ogg': return 'audio/ogg'
    return None


class FileAnalyzer:
    def __init__(self, session_id, temp_base_folder="analysis_temp"):
        self.session_id = session_id
        
        file_scan_keys_str = os.getenv("FILE_SCANNING_GEMINI_KEYS", "")
        primary_keys = [key.strip() for key in file_scan_keys_str.split(',') if key.strip()]
        
        backup_keys = []
        for i in range(1, 10):
            key = os.getenv(f"BACKUP_API_KEY_{i}")
            if key:
                backup_keys.append(key.strip())

        self.api_keys = primary_keys + backup_keys
        
        if not self.api_keys:
            raise ValueError("FileAnalyzer requires API keys. Set FILE_SCANNING_GEMINI_KEYS and/or BACKUP_API_KEY_... in your .env file.")
        
        self.max_generate_retries = len(self.api_keys)
        self.api_key_cycle = itertools.cycle(self.api_keys)
        self.current_api_key = None
        self.uploaded_api_file_names = []
        self.message_queue = queue.Queue()
        self.task_temp_folder = os.path.join(temp_base_folder, f"analyzer_{session_id}_{str(uuid.uuid4())[:8]}")
        os.makedirs(self.task_temp_folder, exist_ok=True)
        logger.info(f"Analyzer {self.session_id}: Initialized task with temp folder {self.task_temp_folder}")

        if not self._rotate_api_key():
            raise ConnectionError(f"Analyzer {self.session_id}: Failed to configure any initial API key.")

    def get_message_queue(self):
        return self.message_queue

    def analyze_file(self, filepath, user_query=""):
        filename = os.path.basename(filepath)
        logger.info(f"Analyzer {self.session_id}: Starting analysis task for '{filename}' with query context.")
        final_status = "UNKNOWN"
        combined_analysis = "[Analysis not started or failed early]"

        try:
            self.message_queue.put({ "type": "file_start", "session_id": self.session_id, "filename": filename })

            should_chunk, file_type = self._check_if_chunking_needed(filepath)

            if should_chunk:
                chunk_folder = os.path.join(self.task_temp_folder, f"{os.path.splitext(filename)[0]}_chunks_temp")
                os.makedirs(chunk_folder, exist_ok=True)
                chunk_files = []
                analysis_results_list = []
                try:
                    self.message_queue.put({"type": "progress", "session_id": self.session_id, "filename": filename, "message": f"Splitting file..."})
                    if file_type == 'pdf':
                        chunk_files = self._split_pdf(filepath, chunk_folder)
                    elif file_type == 'text':
                        chunk_files = self._split_text(filepath, chunk_folder)

                    if not chunk_files:
                        if os.path.exists(filepath) and os.path.getsize(filepath) < 50:
                             final_status = "SKIPPED (File likely empty or too small to chunk)"
                        else:
                             final_status = "FAILED (Splitting yielded no processable chunks)"
                        logger.warning(f"Analyzer {self.session_id}: '{filename}' - {final_status}")
                        self.message_queue.put({"type": "file_error", "session_id": self.session_id, "filename": filename, "error": final_status})
                        combined_analysis = f"[{final_status}]"
                    else:
                        self.message_queue.put({"type": "file_chunk_info", "session_id": self.session_id, "filename": filename, "total_chunks": len(chunk_files)})
                        last_analysis_summary = ""
                        failed_chunks = 0
                        successful_chunks = 0
                        total_chunks = len(chunk_files)

                        for i, chunk_filepath in enumerate(chunk_files):
                            chunk_num = i + 1
                            chunk_filename_only = os.path.basename(chunk_filepath)
                            log_chunk_display = f"'{filename}' (Chunk {chunk_num}/{total_chunks})"

                            self.message_queue.put({"type": "progress", "session_id": self.session_id, "filename": filename, "chunk_num": chunk_num, "total_chunks": total_chunks, "message": f"Uploading chunk {chunk_num}/{total_chunks}..."})
                            uploaded_chunk_obj, upload_err = self._upload_file_with_retry(chunk_filepath, chunk_filename_only)

                            if upload_err:
                                failed_chunks += 1
                                error_msg = f"Upload Failed chunk {chunk_num}: {upload_err}"
                                logger.error(f"Analyzer {self.session_id}: {error_msg}")
                                self.message_queue.put({"type": "chunk_error", "session_id": self.session_id, "filename": filename, "chunk_num": chunk_num, "total_chunks": total_chunks, "error": error_msg})
                                last_analysis_summary = f"[Chunk {chunk_num} upload failed: {upload_err}]"
                                if "Invalid API Key" in upload_err or "All API Keys Failed" in upload_err:
                                    if not self._rotate_api_key():
                                        final_status = "FAILED (All API Keys Failed during chunk upload)"
                                        logger.critical(f"Analyzer {self.session_id}: {final_status}")
                                        break
                                continue

                            active_chunk_obj, wait_err = self._wait_for_file_active(uploaded_chunk_obj, log_chunk_display)
                            if wait_err:
                                failed_chunks += 1
                                error_msg = f"Chunk {chunk_num} Processing Failed: {wait_err}"
                                logger.error(f"Analyzer {self.session_id}: {error_msg}")
                                self.message_queue.put({"type": "chunk_error", "session_id": self.session_id, "filename": filename, "chunk_num": chunk_num, "total_chunks": total_chunks, "error": error_msg})
                                last_analysis_summary = f"[Chunk {chunk_num} processing failed: {wait_err}]"
                                self._cleanup_specific_api_file(uploaded_chunk_obj.name)
                                continue

                            self.message_queue.put({"type": "progress", "session_id": self.session_id, "filename": filename, "chunk_num": chunk_num, "total_chunks": total_chunks, "message": f"Analyzing chunk {chunk_num}/{total_chunks}..."})
                            analysis_text, analysis_err = self._generate_analysis_for_file(
                                active_chunk_obj, filename, is_chunk=True, chunk_num=chunk_num,
                                total_chunks=total_chunks, previous_analysis_summary=last_analysis_summary,
                                user_query=user_query
                            )

                            if analysis_err:
                                failed_chunks += 1
                                error_msg = f"Analysis Failed chunk {chunk_num}: {analysis_err}"
                                logger.error(f"Analyzer {self.session_id}: {error_msg}")
                                last_analysis_summary = f"[Chunk {chunk_num} analysis failed: {analysis_err}]"
                                if "Invalid API Key" in analysis_err or "All API Keys Failed" in analysis_err:
                                     if not self._rotate_api_key():
                                        final_status = "FAILED (All API Keys Failed during chunk analysis)"
                                        logger.critical(f"Analyzer {self.session_id}: {final_status}")
                                        break
                            else:
                                successful_chunks += 1
                                analysis_results_list.append(f"--- Chunk {chunk_num}/{total_chunks} Analysis ---\n{analysis_text if analysis_text else '[No content generated]'}")
                                summary_limit = 500
                                last_analysis_summary = (analysis_text[:summary_limit] + '...') if analysis_text and len(analysis_text) > summary_limit else analysis_text
                                if not last_analysis_summary: last_analysis_summary = "[Previous chunk analysis yielded no text]"


                        if final_status == "UNKNOWN":
                            if failed_chunks == 0 and successful_chunks == total_chunks:
                                final_status = "SUCCESS (Chunked)"
                            elif successful_chunks > 0:
                                final_status = f"PARTIAL ({failed_chunks}/{total_chunks} chunks failed)"
                            else:
                                final_status = f"FAILED (All {total_chunks} chunks failed)"

                        combined_analysis = "\n\n".join(analysis_results_list) if analysis_results_list else "[No analysis generated from any successful chunks]"

                except Exception as split_err:
                    final_status = f"FAILED (Splitting Error: {split_err})"
                    logger.exception(f"Analyzer {self.session_id}: Splitting error for '{filename}'")
                    self.message_queue.put({"type": "file_error", "session_id": self.session_id, "filename": filename, "error": final_status})
                    combined_analysis = f"[Error during file splitting: {split_err}]"
                finally:
                    if os.path.exists(chunk_folder):
                        shutil.rmtree(chunk_folder, ignore_errors=True)
                        logger.debug(f"Analyzer {self.session_id}: Removed chunk temp folder '{chunk_folder}'")

            else:
                self.message_queue.put({"type": "progress", "session_id": self.session_id, "filename": filename, "message": "Uploading..."})
                uploaded_file_obj, upload_err = self._upload_file_with_retry(filepath, filename)

                if upload_err:
                    final_status = f"FAILED (Upload Error: {upload_err})"
                    logger.error(f"Analyzer {self.session_id}: {final_status} for '{filename}'")
                    self.message_queue.put({"type": "file_error", "session_id": self.session_id, "filename": filename, "error": final_status})
                    combined_analysis = f"[File upload failed: {upload_err}]"
                    if "Invalid API Key" in upload_err or "All API Keys Failed" in upload_err:
                         if not self._rotate_api_key():
                             final_status = "FAILED (All API Keys Failed)"
                else:
                    active_file_obj, wait_err = self._wait_for_file_active(uploaded_file_obj, filename)
                    if wait_err:
                        final_status = f"FAILED (File Processing Error: {wait_err})"
                        logger.error(f"Analyzer {self.session_id}: {final_status} for '{filename}'")
                        self.message_queue.put({"type": "file_error", "session_id": self.session_id, "filename": filename, "error": final_status})
                        combined_analysis = f"[File processing failed: {wait_err}]"
                        self._cleanup_specific_api_file(uploaded_file_obj.name)
                    else:
                        self.message_queue.put({"type": "progress", "session_id": self.session_id, "filename": filename, "message": "Analyzing..."})
                        analysis_text, analysis_err = self._generate_analysis_for_file(active_file_obj, filename, is_chunk=False, user_query=user_query)

                        if analysis_err:
                            final_status = f"FAILED (Analysis Error: {analysis_err})"
                            logger.error(f"Analyzer {self.session_id}: {final_status} for '{filename}'")
                            combined_analysis = f"[File analysis failed: {analysis_err}]"
                            if "Invalid API Key" in analysis_err or "All API Keys Failed" in analysis_err:
                                if not self._rotate_api_key():
                                    final_status = "FAILED (All API Keys Failed)"
                        else:
                            final_status = "SUCCESS"
                            combined_analysis = analysis_text if analysis_text else "[Analysis generated no text]"

        except Exception as e:
            final_status = f"FAILED (Unexpected Error: {e})"
            logger.exception(f"Analyzer {self.session_id}: Unexpected error during analysis of '{filename}'")
            self.message_queue.put({"type": "file_error", "session_id": self.session_id, "filename": filename, "error": final_status})
            combined_analysis = f"[An unexpected error occurred during processing: {e}]"

        finally:
            logger.info(f"Analyzer {self.session_id}: Reaching final block for '{filename}'. Status: {final_status}")
            self.message_queue.put({
                "type": "file_complete",
                "session_id": self.session_id,
                "filename": filename,
                "status": final_status,
                "combined_analysis": combined_analysis
            })

            try:
                self.message_queue.put(None)
                logger.info(f"Analyzer {self.session_id}: End signal (None) put on queue for '{filename}'.")
            except Exception as q_err:
                logger.error(f"Analyzer {self.session_id}: Error putting None signal on queue for {filename}: {q_err}")

            self._cleanup_api_files()
            self._cleanup_temp_folder()

            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    logger.info(f"Analyzer {self.session_id}: Deleted local input file '{filepath}'")
                except OSError as rm_err:
                    logger.error(f"Analyzer {self.session_id}: Failed deleting original input file '{filepath}': {rm_err}")

            logger.info(f"Analyzer {self.session_id}: Finished analysis task for '{filename}' with status: {final_status}")

    def _configure_genai_client(self, api_key):
        try:
            genai.configure(api_key=api_key)
            self.current_api_key = api_key
            logger.info(f"Analyzer {self.session_id}: Configured GenAI with key ending ...{api_key[-4:]}")
            return True
        except Exception as e:
            logger.error(f"Analyzer {self.session_id}: Failed to configure key ...{api_key[-4:]}: {e}")
            self.current_api_key = None
            return False

    def _rotate_api_key(self):
        if not self.api_key_cycle:
            logger.error(f"Analyzer {self.session_id}: No key cycle available.")
            return None
        current_key_info = f"...{self.current_api_key[-4:]}" if self.current_api_key else 'N/A'
        for _ in range(len(self.api_keys)):
            new_key = next(self.api_key_cycle)
            if new_key == self.current_api_key and len(self.api_keys) > 1:
                new_key = next(self.api_key_cycle)
            if self._configure_genai_client(new_key):
                logger.warning(f"Analyzer {self.session_id}: Rotated from key {current_key_info} to key ...{new_key[-4:]}")
                return new_key
            else:
                logger.warning(f"Analyzer {self.session_id}: Key ...{new_key[-4:]} failed configuration during rotation.")
        logger.error(f"Analyzer {self.session_id}: All keys failed configuration during rotation attempt.")
        self.current_api_key = None
        return None

    def _handle_api_error(self, e, operation_name="API"):
        error_type = "Unknown Error"
        error_message = str(e).lower()
        current_key_info = f"...{self.current_api_key[-4:]}" if self.current_api_key else 'UNKNOWN'
        if isinstance(e, generation_types.StopCandidateException): error_type = f"Generation Stopped ({e})"
        elif isinstance(e, google_exceptions.InvalidArgument):
            if "api key not valid" in error_message: error_type = "Invalid API Key"
            elif "model" in error_message and ("not found" in error_message or "does not exist" in error_message or "permission" in error_message): error_type = f"Model Not Found/Forbidden ({MODEL_NAME})"
            elif "file processing failed" in error_message: error_type = "File Processing Failed"
            elif "unsupported file format" in error_message or ("mimetype" in error_message and "not supported" in error_message): error_type = "Unsupported File Format/MIME Type"
            elif "user location is not supported" in error_message: error_type = "Location Not Supported"
            elif "is not in an active state" in error_message: error_type = "File Not ACTIVE"
            else: error_type = f"Invalid Argument ({e})"
        elif isinstance(e, google_exceptions.ResourceExhausted): error_type = "Resource Exhausted (Quota/Rate Limit)"
        elif isinstance(e, google_exceptions.PermissionDenied): error_type = f"Permission Denied ({e})"
        elif isinstance(e, google_exceptions.DeadlineExceeded): error_type = "Deadline Exceeded (Timeout)"
        elif isinstance(e, google_exceptions.ServiceUnavailable): error_type = "Service Unavailable"
        elif isinstance(e, google_exceptions.InternalServerError): error_type = "Internal Server Error"
        elif isinstance(e, google_exceptions.NotFound): error_type = f"Not Found ({e})"
        elif isinstance(e, google_exceptions.FailedPrecondition): error_type = f"Failed Precondition ({e})"
        elif isinstance(e, google_exceptions.GoogleAPICallError): error_type = f"Google API Call Error ({e})"
        logger.warning(f"Analyzer {self.session_id}: {operation_name} Error ({error_type}) with key {current_key_info}: {e}")
        return error_type

    def _upload_file_with_retry(self, filepath, filename):
        upload_retries = 0
        last_error_type = "No Attempts Made"
        mime_type = get_mime_type(filename)
        logger.debug(f"Analyzer {self.session_id}: Attempting upload for '{filename}' (MIME: '{mime_type or 'API Default'}')")
        while upload_retries <= MAX_UPLOAD_RETRIES:
            if not self.current_api_key:
                logger.error(f"Analyzer {self.session_id}: Cannot upload '{filename}', no valid API key.")
                return None, "Configuration Error (No Key)"
            try:
                time.sleep(RATE_LIMIT_DELAY_UPLOAD)
                logging.info(f"Analyzer {self.session_id}: Uploading '{filename}' (Attempt {upload_retries + 1}/{MAX_UPLOAD_RETRIES + 1})")
                upload_kwargs = {'path': filepath, 'display_name': filename}
                if mime_type: upload_kwargs['mime_type'] = mime_type
                uploaded_file = genai.upload_file(**upload_kwargs)
                logging.info(f"Analyzer {self.session_id}: Uploaded '{filename}' as API resource '{uploaded_file.name}'")
                self.uploaded_api_file_names.append(uploaded_file.name)
                return uploaded_file, None
            except (google_exceptions.GoogleAPICallError, google_exceptions.RetryError) as e:
                last_error_type = self._handle_api_error(e, f"File Upload ('{filename}')")
                if last_error_type == "Invalid API Key":
                    if not self._rotate_api_key(): return None, "All API Keys Failed"
                    continue
                elif last_error_type in ["Resource Exhausted (Quota/Rate Limit)", "Service Unavailable", "Internal Server Error", "Deadline Exceeded (Timeout)"]:
                    upload_retries += 1
                    wait_time = RATE_LIMIT_DELAY_UPLOAD * (2 ** upload_retries)
                    logging.info(f"Analyzer {self.session_id}: Retrying upload '{filename}' in {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue
                elif last_error_type == "Unsupported File Format/MIME Type": return None, last_error_type
                else: return None, last_error_type
            except Exception as e:
                upload_retries += 1
                last_error_type = f"Unexpected Upload Error: {str(e)}"
                logger.exception(f"Analyzer {self.session_id}: Unexpected upload error '{filename}' (Attempt {upload_retries}/{MAX_UPLOAD_RETRIES + 1})")
                if upload_retries > MAX_UPLOAD_RETRIES: return None, last_error_type
                wait_time = RATE_LIMIT_DELAY_UPLOAD * (2 ** upload_retries)
                time.sleep(wait_time)
        return None, f"Max Retries Hit ({last_error_type})"

    def _wait_for_file_active(self, uploaded_file_api_object, original_filename):
        if not uploaded_file_api_object or not hasattr(uploaded_file_api_object, 'name'):
            logger.error(f"Analyzer {self.session_id}: Invalid file object passed to _wait_for_file_active for '{original_filename}'")
            return None, "Invalid file object for waiting"
        file_name = uploaded_file_api_object.name
        start_time = time.time()
        last_state = "UNKNOWN"
        self.message_queue.put({
            "type": "progress", "session_id": self.session_id, "filename": original_filename,
            "message": "Waiting for file processing..."
        })
        while time.time() - start_time < FILE_PROCESSING_WAIT_TIMEOUT_SECONDS:
            try:
                current_file_status = genai.get_file(name=file_name)
                last_state = current_file_status.state.name
                logger.debug(f"Analyzer {self.session_id}: Polling state for '{original_filename}' ({file_name}). Current state: {last_state}")
                if last_state == "ACTIVE":
                    logger.info(f"Analyzer {self.session_id}: File '{original_filename}' ({file_name}) is ACTIVE.")
                    self.message_queue.put({
                        "type": "progress", "session_id": self.session_id, "filename": original_filename,
                        "message": "File processing complete."
                    })
                    return current_file_status, None
                elif last_state == "FAILED":
                    logger.error(f"Analyzer {self.session_id}: File '{original_filename}' ({file_name}) FAILED processing.")
                    return None, "File Processing Failed by API"
                elif last_state == "PROCESSING":
                    time.sleep(FILE_PROCESSING_POLL_INTERVAL_SECONDS)
                    continue
                else:
                    logger.warning(f"Analyzer {self.session_id}: File '{original_filename}' ({file_name}) in unexpected state: {last_state}")
                    return None, f"File in unexpected state: {last_state}"
            except google_exceptions.NotFound:
                 logger.error(f"Analyzer {self.session_id}: File '{original_filename}' ({file_name}) not found during status polling. Maybe deleted?")
                 return None, "File Not Found during polling"
            except google_exceptions.GoogleAPICallError as e:
                 error_type = self._handle_api_error(e, f"Polling File State ('{original_filename}')")
                 logger.error(f"Analyzer {self.session_id}: API error polling file '{original_filename}' ({file_name}): {error_type}")
                 if error_type == "Invalid API Key":
                     if not self._rotate_api_key(): return None, "All API Keys Failed during polling"
                     continue
                 elif error_type in ["Resource Exhausted (Quota/Rate Limit)", "Service Unavailable", "Internal Server Error", "Deadline Exceeded (Timeout)"]:
                     time.sleep(FILE_PROCESSING_POLL_INTERVAL_SECONDS * 2)
                     continue
                 else:
                     return None, f"API Error polling file state: {error_type}"
            except Exception as e:
                logger.exception(f"Analyzer {self.session_id}: Unexpected error polling file '{original_filename}' ({file_name}): {e}")
                return None, f"Unexpected error polling file state: {str(e)}"
        logger.error(f"Analyzer {self.session_id}: Timeout waiting for file '{original_filename}' ({file_name}) to become ACTIVE. Last state: {last_state}")
        return None, f"Timeout waiting for file processing (last state: {last_state})"

    def _generate_analysis_for_file(self, file_object, original_filename, is_chunk=False, chunk_num=0, total_chunks=0, previous_analysis_summary="", user_query=""):
        generate_retries = 0
        last_error_type = "No Attempts Made"
        safety_settings = {
            'HARM_CATEGORY_HARASSMENT': 'BLOCK_ONLY_HIGH',
            'HARM_CATEGORY_HATE_SPEECH': 'BLOCK_ONLY_HIGH',
            'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'BLOCK_ONLY_HIGH',
            'HARM_CATEGORY_DANGEROUS_CONTENT': 'BLOCK_ONLY_HIGH',
        }
        generation_config = {"temperature": 0.7}
        api_display_name = file_object.display_name if hasattr(file_object, 'display_name') and file_object.display_name else file_object.name
        log_filename_display = f"'{original_filename}' (Chunk {chunk_num}/{total_chunks})" if is_chunk else f"'{original_filename}'"
        
        safe_query = user_query if user_query else "No specific query provided. Perform general analysis."
        
        if is_chunk:
            prompt_template = FIRST_CHUNK_ANALYSIS_PROMPT if chunk_num == 1 else CHUNK_ANALYSIS_PROMPT
            prompt = prompt_template.format(
                filename=api_display_name,
                original_filename=werkzeug.utils.secure_filename(original_filename),
                chunk_num=chunk_num,
                total_chunks=total_chunks,
                prev_chunk_num=chunk_num - 1,
                previous_analysis_summary=previous_analysis_summary if previous_analysis_summary else "No context provided from previous chunk.",
                user_query=safe_query
            )
        else:
            prompt = FILE_ANALYSIS_PROMPT.format(
                filename=api_display_name,
                user_query=safe_query
            )
        contents = [prompt, file_object]
        while generate_retries <= self.max_generate_retries:
            if not self.current_api_key:
                error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": "Configuration Error (No Key)"}
                if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                self.message_queue.put(error_payload)
                return None, "Configuration Error (No Key)"
            try:
                time.sleep(RATE_LIMIT_DELAY_GENERATE)
                logging.info(f"Analyzer {self.session_id}, File {log_filename_display}: Generating analysis (Attempt {generate_retries + 1}/{self.max_generate_retries + 1})")
                model = genai.GenerativeModel(MODEL_NAME, safety_settings=safety_settings, generation_config=generation_config)
                response = model.generate_content(contents=contents, request_options={'timeout': 600})
                analysis_text = ""; finish_reason_str = "UNKNOWN"
                if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                    block_reason = getattr(response.prompt_feedback, 'block_reason', None)
                    if block_reason and block_reason != 0:
                        reason_name = block_reason.name if hasattr(block_reason, 'name') else str(block_reason); finish_reason_str = f"BLOCKED ({reason_name})"
                        block_message = getattr(response.prompt_feedback, 'block_reason_message', '')
                        logging.warning(f"Analyzer {self.session_id}: Analysis {log_filename_display} blocked. Reason: {reason_name}, Msg: {block_message}")
                        error_detail = f"Content Generation Blocked (Reason: {reason_name})"; last_error_type = "Content Blocked"
                        error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": error_detail}
                        if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                        self.message_queue.put(error_payload)
                        return None, error_detail
                if hasattr(response, 'candidates') and response.candidates:
                    finish_reason_obj = getattr(response.candidates[0], 'finish_reason', 'UNKNOWN')
                    finish_reason_str = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                    if finish_reason_str in ['STOP', 'MAX_TOKENS', 'FINISH_REASON_UNSPECIFIED', 'UNSPECIFIED', 'MODEL_TERMINATION']:
                        if hasattr(response, 'text'): analysis_text = response.text
                        elif hasattr(response.candidates[0], 'content') and hasattr(response.candidates[0].content, 'parts'):
                            analysis_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                    else: logging.warning(f"Analyzer {self.session_id}: Analysis {log_filename_display} finished with reason: {finish_reason_str}")
                if not analysis_text or not analysis_text.strip():
                    logging.warning(f"Analyzer {self.session_id}: Analysis {log_filename_display} resulted in empty text. Finish Reason: {finish_reason_str}")
                    analysis_text = "[No text content generated]"
                result_payload = {"session_id": self.session_id, "filename": original_filename}
                if is_chunk: result_payload.update({"type": "chunk_result", "chunk_num": chunk_num, "total_chunks": total_chunks, "analysis": analysis_text})
                else: result_payload.update({"type": "file_result", "analysis": analysis_text})
                self.message_queue.put(result_payload)
                logging.info(f"Analyzer {self.session_id}: Generated analysis for {log_filename_display}")
                return analysis_text, None
            except (google_exceptions.GoogleAPICallError, google_exceptions.RetryError) as e:
                last_error_type = self._handle_api_error(e, f"Content Generation ({log_filename_display})")
                if last_error_type == "File Not ACTIVE":
                     logger.error(f"Analyzer {self.session_id}: Received 'File Not ACTIVE' error *during* generation for {log_filename_display}, despite waiting. This is unexpected.")
                     error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": last_error_type}
                     if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                     self.message_queue.put(error_payload)
                     return None, last_error_type
                elif last_error_type == "Invalid API Key":
                    if not self._rotate_api_key():
                        error_msg = "All API Keys Failed during generation."
                        error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": error_msg}
                        if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                        self.message_queue.put(error_payload)
                        return None, error_msg
                    continue
                elif last_error_type in ["Resource Exhausted (Quota/Rate Limit)", "Service Unavailable", "Internal Server Error", "Deadline Exceeded (Timeout)"]:
                    generate_retries += 1
                    wait_time = RATE_LIMIT_DELAY_GENERATE * (2 ** generate_retries)
                    logging.info(f"Analyzer {self.session_id}: Retrying generation {log_filename_display} in {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": last_error_type}
                    if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                    self.message_queue.put(error_payload)
                    return None, last_error_type
            except Exception as e:
                generate_retries += 1
                last_error_type = f"Unexpected Generation Error: {str(e)}"
                logger.exception(f"Analyzer {self.session_id}: Unexpected error generating {log_filename_display} (Attempt {generate_retries}/{self.max_generate_retries + 1})")
                if generate_retries > self.max_generate_retries:
                    error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": last_error_type}
                    if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
                    self.message_queue.put(error_payload)
                    return None, last_error_type
                wait_time = RATE_LIMIT_DELAY_GENERATE * (2 ** generate_retries)
                time.sleep(wait_time)
        final_error_msg = f"Max Retries Hit ({last_error_type})"
        error_payload = {"type": "file_error", "session_id": self.session_id, "filename": original_filename, "error": final_error_msg}
        if is_chunk: error_payload.update({"chunk_num": chunk_num, "total_chunks": total_chunks, "type": "chunk_error"})
        self.message_queue.put(error_payload)
        return None, final_error_msg

    def _check_if_chunking_needed(self, filepath):
        try:
            file_size = os.path.getsize(filepath)
            filename = os.path.basename(filepath)
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext == '.pdf':
                size_mb = file_size / (1024 * 1024)
                if size_mb > PDF_CHUNK_SIZE_LIMIT_MB:
                    logger.info(f"Analyzer {self.session_id}: PDF '{filename}' ({size_mb:.2f} MB) requires chunking (size limit).")
                    return True, 'pdf'
                try:
                    reader = PdfReader(filepath)
                    total_pages = len(reader.pages)
                    if total_pages > PDF_CHUNK_PAGE_LIMIT:
                        logger.info(f"Analyzer {self.session_id}: PDF '{filename}' ({total_pages} pages) requires chunking (page limit).")
                        return True, 'pdf'
                except Exception as pdf_read_err:
                    logger.warning(f"Analyzer {self.session_id}: Could not read PDF pages for '{filename}' to check page limit: {pdf_read_err}")
                return False, 'pdf'
            elif file_ext in ['.txt', '.csv', '.md', '.json', '.html', '.xml', '.log', '.py', '.js', '.css', '.java', '.c', '.cpp', '.h', '.rb', '.php', '.go', '.rs', '.swift', '.kt']:
                 try:
                    encodings_to_try = ['utf-8', 'latin-1', 'cp1252']
                    content = None
                    for enc in encodings_to_try:
                        try:
                            with open(filepath, 'r', encoding=enc) as f: content = f.read()
                            break
                        except UnicodeDecodeError: continue
                        except Exception: break
                    if content is None:
                        logger.warning(f"Analyzer {self.session_id}: Could not decode '{filename}' to check char count.")
                        return False, 'text'
                    char_count = len(content)
                    if char_count > TEXT_CHUNK_CHAR_LIMIT:
                        logger.info(f"Analyzer {self.session_id}: Text file '{filename}' ({char_count:,} chars) requires chunking.")
                        return True, 'text'
                 except OSError as e:
                    logger.error(f"Analyzer {self.session_id}: Error reading '{filename}' for chunk check: {e}")
                    return False, 'text'
        except OSError as e: logger.error(f"Analyzer {self.session_id}: Error accessing '{filepath}' for chunk check: {e}")
        except Exception as e_generic: logger.error(f"Analyzer {self.session_id}: Unexpected error checking chunking for '{filepath}': {e_generic}")
        return False, None

    def _split_pdf(self, filepath, chunk_folder):
        chunk_files = []; original_filename_base = os.path.splitext(os.path.basename(filepath))[0]; max_bytes = PDF_CHUNK_SIZE_LIMIT_MB * 1024 * 1024
        try:
            reader = PdfReader(filepath)
            total_pages = len(reader.pages)
            if total_pages == 0: return []
            current_page_index = 0; chunk_num = 1
            while current_page_index < total_pages:
                 writer = PdfWriter(); pages_in_this_chunk = 0; chunk_page_indices = []
                 for page_idx in range(current_page_index, total_pages):
                    if pages_in_this_chunk >= PDF_CHUNK_PAGE_LIMIT:
                        logger.debug(f"Analyzer {self.session_id}: Chunk {chunk_num} reached page limit ({PDF_CHUNK_PAGE_LIMIT}). Starting new chunk.")
                        break
                    try:
                        page = reader.pages[page_idx]; temp_writer = PdfWriter()
                        for confirmed_idx in chunk_page_indices:
                            try: temp_writer.add_page(reader.pages[confirmed_idx])
                            except IndexError: pass
                        temp_writer.add_page(page); temp_buffer = io.BytesIO(); potential_size = 0
                        try: temp_writer.write(temp_buffer); potential_size = temp_buffer.tell()
                        except Exception: potential_size = max_bytes + 1
                        finally: temp_buffer.close()
                        
                        if potential_size > max_bytes:
                            if pages_in_this_chunk == 0: logger.warning(f"Analyzer {self.session_id}: PDF Page {page_idx+1} alone exceeds size limit. Skipping page."); current_page_index = page_idx + 1; break
                            else: break
                        
                        writer.add_page(page); pages_in_this_chunk += 1; chunk_page_indices.append(page_idx)
                    except Exception as page_err:
                        logger.error(f"Analyzer {self.session_id}: Error processing PDF page {page_idx+1}: {page_err}. Skipping.")
                        if pages_in_this_chunk == 0: current_page_index = page_idx + 1
                        continue
                 if pages_in_this_chunk > 0:
                     chunk_filename = f"{original_filename_base}_chunk_{chunk_num}.pdf"; chunk_filepath = os.path.join(chunk_folder, chunk_filename)
                     try:
                         with open(chunk_filepath, "wb") as f_chunk: writer.write(f_chunk)
                         size_mb = os.path.getsize(chunk_filepath) / (1024*1024); logger.info(f"Analyzer {self.session_id}: Created PDF chunk {chunk_num} ('{chunk_filename}', {pages_in_this_chunk} pages, {size_mb:.2f} MB)")
                         chunk_files.append(chunk_filepath); current_page_index = chunk_page_indices[-1] + 1; chunk_num += 1
                     except Exception as write_err: logger.error(f"Analyzer {self.session_id}: Failed to write PDF chunk {chunk_num}: {write_err}"); current_page_index = total_pages; break
                 elif current_page_index < total_pages:
                     if current_page_index <= (chunk_page_indices[-1] if chunk_page_indices else current_page_index): current_page_index += 1
        except Exception as e: logger.exception(f"Analyzer {self.session_id}: Error splitting PDF '{filepath}'"); raise
        return chunk_files

    def _split_text(self, filepath, chunk_folder):
        chunk_files = []; original_filename_base, original_ext = os.path.splitext(os.path.basename(filepath)); max_chars = TEXT_CHUNK_CHAR_LIMIT
        try:
            encodings_to_try = ['utf-8', 'latin-1', 'cp1252']; content = None; file_encoding = None
            for enc in encodings_to_try:
                try:
                    with open(filepath, 'r', encoding=enc) as f: content = f.read()
                    file_encoding = enc; break
                except UnicodeDecodeError: continue
                except Exception as read_err: raise read_err
            if content is None: raise ValueError("Could not decode file for text splitting.")
            total_chars = len(content); start_char = 0; chunk_num = 1
            if total_chars == 0: return []
            while start_char < total_chars:
                end_char = min(start_char + max_chars, total_chars); last_newline = content.rfind('\n', start_char, end_char)
                if last_newline > start_char and last_newline < end_char - 1: end_char = last_newline + 1
                chunk_content = content[start_char:end_char]
                if not chunk_content: start_char = end_char; continue
                chunk_filename = f"{original_filename_base}_chunk_{chunk_num}{original_ext}"; chunk_filepath = os.path.join(chunk_folder, chunk_filename)
                try:
                    with open(chunk_filepath, 'w', encoding=file_encoding) as f_chunk: f_chunk.write(chunk_content)
                    logger.info(f"Analyzer {self.session_id}: Created text chunk {chunk_num} ('{chunk_filename}', {len(chunk_content):,} chars)"); chunk_files.append(chunk_filepath)
                except Exception as write_err: logger.error(f"Analyzer {self.session_id}: Failed to write text chunk {chunk_num}: {write_err}"); start_char = end_char; chunk_num += 1; continue
                start_char = end_char; chunk_num += 1
        except Exception as e: logger.exception(f"Analyzer {self.session_id}: Error splitting text file '{filepath}'"); raise
        return chunk_files

    def _cleanup_specific_api_file(self, file_api_name_to_delete):
        if not file_api_name_to_delete or not self.current_api_key:
             logger.warning(f"Analyzer {self.session_id}: Cannot clean up specific API file {file_api_name_to_delete} - missing name or API key.")
             return
        logger.info(f"Analyzer {self.session_id}: Attempting cleanup of specific failed API file '{file_api_name_to_delete}'...")
        if not self._configure_genai_client(self.current_api_key):
            logger.error(f"Analyzer {self.session_id}: Failed to reconfigure client for specific API file cleanup.")
            if file_api_name_to_delete in self.uploaded_api_file_names:
                self.uploaded_api_file_names.remove(file_api_name_to_delete)
            return
        try:
            time.sleep(0.5)
            genai.delete_file(name=file_api_name_to_delete)
            logger.info(f"Analyzer {self.session_id}: Deleted specific API file '{file_api_name_to_delete}'.")
        except google_exceptions.NotFound:
            logger.warning(f"Analyzer {self.session_id}: Specific API file '{file_api_name_to_delete}' not found during cleanup.")
        except Exception as e_del:
            self._handle_api_error(e_del, f"Specific API File Delete ('{file_api_name_to_delete}')")
        if file_api_name_to_delete in self.uploaded_api_file_names:
            try:
                self.uploaded_api_file_names.remove(file_api_name_to_delete)
            except ValueError:
                 pass

    def _cleanup_api_files(self):
        if not self.uploaded_api_file_names:
            logger.debug(f"Analyzer {self.session_id}: No remaining API files to clean up.")
            return
        if not self.current_api_key:
             logger.error(f"Analyzer {self.session_id}: Cannot cleanup API files, no valid key available.")
             return
        logger.info(f"Analyzer {self.session_id}: Cleaning up {len(self.uploaded_api_file_names)} remaining API files...")
        if not self._configure_genai_client(self.current_api_key):
            logger.error(f"Analyzer {self.session_id}: Failed to reconfigure client for final API file cleanup.")
            return
        delete_failures = 0
        files_to_attempt_delete = list(self.uploaded_api_file_names)
        for file_name in files_to_attempt_delete:
            try:
                time.sleep(0.5)
                logger.debug(f"Analyzer {self.session_id}: Deleting API file '{file_name}'...")
                genai.delete_file(name=file_name)
                logger.info(f"Analyzer {self.session_id}: Deleted API file '{file_name}'.")
                if file_name in self.uploaded_api_file_names:
                     self.uploaded_api_file_names.remove(file_name)
            except google_exceptions.NotFound:
                logger.warning(f"Analyzer {self.session_id}: API file '{file_name}' not found during cleanup.")
                if file_name in self.uploaded_api_file_names:
                     self.uploaded_api_file_names.remove(file_name)
            except Exception as e_del:
                delete_failures += 1
                self._handle_api_error(e_del, f"API File Delete ('{file_name}')")
        if delete_failures > 0:
            logger.warning(f"Analyzer {self.session_id}: Failed to delete {delete_failures} API files. Remaining tracked: {self.uploaded_api_file_names}")
        else:
            logger.info(f"Analyzer {self.session_id}: API file cleanup successful. Remaining tracked: {len(self.uploaded_api_file_names)}")

    def _cleanup_temp_folder(self):
        if os.path.exists(self.task_temp_folder):
            try:
                shutil.rmtree(self.task_temp_folder)
                logger.info(f"Analyzer {self.session_id}: Removed task temp folder '{self.task_temp_folder}'")
            except OSError as e:
                logger.error(f"Analyzer {self.session_id}: Failed to remove task temp folder '{self.task_temp_folder}': {e}")
        else:
            logger.debug(f"Analyzer {self.session_id}: Task temp folder '{self.task_temp_folder}' not found for cleanup.")

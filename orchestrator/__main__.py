# __main__.py
import sys
import os
import logging
import signal
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime

import orchestrator.config as config
import orchestrator.container as container
from orchestrator.engine import OrchestratorEngine

def setup_logging():
    # Ensure log directory exists
    os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
    
    logger = logging.getLogger("stellar-orchestrator")
    logger.setLevel(logging.INFO)
    
    # Formatters
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
    
    # File handler
    file_handler = RotatingFileHandler(config.LOG_PATH, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

def main():
    logger = setup_logging()
    
    logger.info("="*60)
    logger.info("Stellar Autonomous Agent Orchestrator Starting Up...")
    logger.info(f"Database Path: {config.DB_PATH}")
    logger.info(f"Log File: {config.LOG_PATH}")
    logger.info(f"Target Container: {config.CONTAINER_NAME}")
    logger.info("="*60)
    logger.info("Agent Execution Pipeline:")
    for i, a in enumerate(config.AGENT_PIPELINE, 1):
        logger.info(f"  {i}. {a['name']} ({a['role']}) - Scheduled: {a['schedule']} IST - Prompt: {a['prompt_file']}")
    logger.info("="*60)

    engine = OrchestratorEngine()

    def handle_shutdown(signum, frame):
        logger.info(f"Shutdown signal ({signum}) received. Initiating graceful shutdown...")
        if engine.current_process or engine.current_run_id:
            logger.info(f"Active run detected (Agent: {engine.current_agent_id}). Terminating...")
            if engine.current_process:
                try:
                    engine.current_process.kill()
                except Exception as e:
                    logger.error(f"Error killing agent process: {e}")
            
            # Kill agy runs inside container
            container.exec_in_container("pkill -f agy")
            
            if engine.current_run_id:
                now_str = datetime.now().isoformat()
                agent_name = "Agent"
                for a in config.AGENT_PIPELINE:
                    if a['id'] == engine.current_agent_id:
                        agent_name = a['name']
                        break
                summary_msg = f"↩️ Agent {agent_name} was interrupted by an orchestrator restart — retrying automatically."
                engine.state_db.interrupt_run(
                    engine.current_run_id, 
                    now_str, 
                    f"Orchestrator shut down via signal {signum}",
                    summary_message=summary_msg
                )
                
        container.unload_agent_prompt()
        logger.info("Shutdown cleanup complete. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start execution loop
    engine.run()

if __name__ == "__main__":
    main()

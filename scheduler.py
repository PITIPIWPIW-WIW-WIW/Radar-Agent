import logging
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scheduler")

INTERVAL_SECONDS = 3 * 24 * 60 * 60  


def run_pipeline_once() -> None:
    """Запускает main.py ОТДЕЛЬНЫМ процессом, а не импортом + вызовом main().

    Планировщик живёт неделями в одном процессе — если бы дергали main.main()
    напрямую в цикле, логирование (setup_logging добавляет FileHandler заново
    при каждом вызове) и любое кэшированное состояние фетчеров копились бы
    без перезапуска. Чистый subprocess = гарантированно чистое состояние
    на каждый цикл, как при обычном ручном запуске `python main.py`.
    """
    logger.info("Запуск полного цикла пайплайна (main.py)...")
    result = subprocess.run([sys.executable, "main.py"])
    if result.returncode != 0:
        logger.error(f"main.py завершился с кодом {result.returncode}")
    else:
        logger.info("Цикл пайплайна завершён успешно.")


def loop() -> None:
    while True:
        try:
            run_pipeline_once()
        except Exception:
            logger.exception("Непредвиденный сбой планировщика — цикл продолжается")

        logger.info(f"Следующий запуск через {INTERVAL_SECONDS // 3600} ч.")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    loop()
import logging

from database import get_all_leaderboard_snapshots, get_latest_analysis, save_analysis
import agent_manager

logger = logging.getLogger("leaderboard_analyzer")


def run_leaderboard_analysis() -> None:
    snapshots = get_all_leaderboard_snapshots()  # от новых к старым
    if not snapshots:
        logger.info("Нет данных лидерборда для анализа.")
        return

    current_snapshot = snapshots[0]
    previous_snapshot = snapshots[1] if len(snapshots) > 1 else None

    prev_analysis_row = get_latest_analysis()
    previous_analysis_text = prev_analysis_row["analysis_text"] if prev_analysis_row else None

    # Если анализ для этого снимка уже построен (например, повторный вызов) —
    # не дублируем работу и не тратим лишний запрос к LLM.
    if prev_analysis_row and prev_analysis_row["fetched_at"] == current_snapshot["fetched_at"]:
        logger.info(f"Анализ для снимка {current_snapshot['fetched_at']} уже существует, пропускаем.")
        return

    try:
        analysis = agent_manager.analyze_leaderboard(
            current_snapshot=current_snapshot,
            previous_snapshot=previous_snapshot,
            previous_analysis=previous_analysis_text,
        )
    except agent_manager.AnalysisError as e:
        logger.error(f"Не удалось построить анализ лидерборда: {e}")
        return

    analysis_text = analysis.summary + "\n\n" + "\n".join(f"- {t}" for t in analysis.trends)
    save_analysis(current_snapshot["fetched_at"], analysis_text)
    logger.info(f"Новый анализ лидерборда сохранён (снимок {current_snapshot['fetched_at']}).")
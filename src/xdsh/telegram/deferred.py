"""Localized labels for append-only background reports, not completion claims."""

from xdsh.core.i18n import canonical_language

_LABELS = {
    "en": ("Background follow-up · {ref}", "parent task unconfirmed", "No report text received."),
    "zh-CN": ("后台补充报告 · {ref}", "原任务归属未确认", "未收到报告正文。"),
    "zh-TW": ("背景補充報告 · {ref}", "原任務歸屬未確認", "未收到報告正文。"),
    "ja": ("バックグラウンド追加報告 · {ref}", "元のタスクは未確認", "報告本文はありません。"),
    "ko": ("백그라운드 추가 보고 · {ref}", "원래 작업 미확인", "보고서 본문이 없습니다."),
    "es": (
        "Informe adicional en segundo plano · {ref}",
        "tarea de origen sin confirmar",
        "Sin texto de informe.",
    ),
    "fr": (
        "Rapport complémentaire en arrière-plan · {ref}",
        "tâche d'origine non confirmée",
        "Aucun texte de rapport reçu.",
    ),
    "de": (
        "Hintergrund-Nachtrag · {ref}",
        "Ursprungsaufgabe unbestätigt",
        "Kein Berichtstext empfangen.",
    ),
    "pt-BR": (
        "Relatório adicional em segundo plano · {ref}",
        "tarefa de origem não confirmada",
        "Nenhum texto de relatório recebido.",
    ),
    "ru": (
        "Дополнительный фоновый отчёт · {ref}",
        "исходная задача не подтверждена",
        "Текст отчёта не получен.",
    ),
    "ar": ("تقرير إضافي في الخلفية · {ref}", "المهمة الأصلية غير مؤكدة", "لم يصل نص التقرير."),
    "hi": ("पृष्ठभूमि पूरक रिपोर्ट · {ref}", "मूल कार्य की पुष्टि नहीं हुई", "रिपोर्ट का पाठ प्राप्त नहीं हुआ।"),
    "id": (
        "Laporan tambahan latar belakang · {ref}",
        "tugas asal belum terkonfirmasi",
        "Teks laporan belum diterima.",
    ),
}


def report_labels(language: str, origin_id: str | None) -> tuple[str, str]:
    title, unknown, empty = _LABELS[canonical_language(language)]
    return title.format(ref=origin_id or unknown), empty


_CANCELLED = {
    "en": "The current session runtime and late-report listener are closed. Saved reports remain.",
    "zh-CN": "已关闭当前会话的运行时和后台报告监听; 已保存的报告保留。",
    "zh-TW": "已關閉目前會話的執行階段與背景報告監聽; 已儲存的報告保留。",
    "ja": "現在のセッションと追加報告の監視を終了しました。保存済みの報告は保持されます。",
    "ko": "현재 세션 런타임과 추가 보고 감시를 종료했습니다. 저장된 보고서는 유지됩니다.",
    "es": "Sesión y escucha de informes cerradas. Los informes guardados se conservan.",
    "fr": "Session et écoute des rapports fermées. Les rapports enregistrés sont conservés.",
    "de": (
        "Sitzungslaufzeit und Berichtsüberwachung geschlossen. "
        "Gespeicherte Berichte bleiben erhalten."
    ),
    "pt-BR": "Sessão e escuta de relatórios encerradas. Os relatórios salvos são mantidos.",
    "ru": "Среда сеанса и наблюдение за отчётами закрыты. Сохранённые отчёты остаются.",
    "ar": "أُغلقت بيئة الجلسة ومراقبة التقارير الإضافية. تبقى التقارير المحفوظة.",
    "hi": "वर्तमान सत्र और अतिरिक्त रिपोर्ट निगरानी बंद कर दी गई है। सहेजी गई रिपोर्ट सुरक्षित हैं।",
    "id": "Runtime sesi dan pemantau laporan ditutup. Laporan tersimpan tetap dipertahankan.",
}


def cancellation_label(language: str) -> str:
    return _CANCELLED[canonical_language(language)]

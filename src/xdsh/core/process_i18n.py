"""Public process display controls."""
# ruff: noqa: E501, RUF001

ROWS = {
    "en": ("Process updates", "Off", "Key", "All", "Applies to new tasks. Batched public excerpts only; no private reasoning. Progress cards rotate after updates."),
    "zh-CN": ("过程消息", "关闭", "关键过程", "全部过程", "对新任务生效。公开过程摘录会合并发送，不含内部推理；发送后轮换进度卡。"),
    "zh-TW": ("過程訊息", "關閉", "關鍵過程", "全部過程", "對新任務生效。公開過程摘錄會合併傳送，不含內部推理；傳送後輪換進度卡。"),
    "ja": ("経過メッセージ", "オフ", "重要", "すべて", "新しいタスクに適用。公開テキスト抜粋をまとめて送信し、進捗カードを置換します。内部推論は非表示。"),
    "ko": ("진행 메시지", "끄기", "주요", "전체", "새 작업에 적용됩니다. 공개 발췌를 모아 보내고 진행 카드를 교체합니다. 내부 추론은 제외됩니다."),
    "es": ("Mensajes de proceso", "Desactivar", "Clave", "Todos", "Para tareas nuevas. Extractos públicos agrupados, sin razonamiento privado. Se renueva la tarjeta de progreso."),
    "fr": ("Messages de progression", "Désactivé", "Essentiels", "Tous", "Pour les nouvelles tâches. Extraits publics regroupés, sans raisonnement privé. La carte de progression est remplacée."),
    "de": ("Prozessmeldungen", "Aus", "Wichtig", "Alle", "Für neue Aufgaben. Gebündelte öffentliche Auszüge ohne internes Denken. Die Fortschrittskarte wird ersetzt."),
    "pt-BR": ("Mensagens de processo", "Desativar", "Principais", "Todas", "Para novas tarefas. Trechos públicos agrupados, sem raciocínio interno. O cartão de progresso é substituído."),
    "ru": ("Сообщения о ходе работы", "Выключено", "Ключевые", "Все", "Для новых задач. Публичные выдержки группируются, внутренние рассуждения скрыты. Карточка прогресса заменяется."),
    "ar": ("رسائل سير العمل", "إيقاف", "المهم", "الكل", "للمهام الجديدة. مقتطفات عامة مجمعة بلا تفكير داخلي. تُستبدل بطاقة التقدم."),
    "hi": ("प्रक्रिया संदेश", "बंद", "मुख्य", "सभी", "नए कार्यों पर लागू। सार्वजनिक अंश एक साथ भेजे जाते हैं; निजी तर्क नहीं। प्रगति कार्ड बदला जाता है।"),
    "id": ("Pesan proses", "Nonaktif", "Penting", "Semua", "Untuk tugas baru. Cuplikan publik digabung, tanpa penalaran internal. Kartu progres diganti."),
}


def process_labels(locale: str) -> tuple[str, str, str, str, str]:
    return ROWS.get(locale, ROWS["en"])

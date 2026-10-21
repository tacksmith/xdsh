"""Resource policy selector labels, independent of transport and credentials."""
# ruff: noqa: E501

LABELS = {
    "en": ("Task resource limits", "Enable limits", "No resource limits", "Applies to new tasks in this chat. Safety and stall protection stay enabled."),
    "zh-CN": ("任务资源上限", "开启限制", "无资源上限", "对本聊天的新任务生效。安全与停滞保护仍开启。"),
    "zh-TW": ("任務資源上限", "開啟限制", "無資源上限", "對本聊天的新任務生效。安全與停滯保護仍開啟。"),
    "ja": ("タスクのリソース上限", "上限を有効化", "リソース上限なし", "このチャットの新しいタスクに適用。安全・停止検知は有効です。"),
    "ko": ("작업 리소스 제한", "제한 켜기", "리소스 제한 없음", "이 채팅의 새 작업에 적용됩니다. 안전 및 정체 감지는 유지됩니다."),
    "es": ("Límites de recursos", "Activar límites", "Sin límites de recursos", "Se aplica a tareas nuevas de este chat. Se mantiene la protección de seguridad y bloqueo."),
    "fr": ("Limites de ressources", "Activer les limites", "Sans limite de ressources", "Pour les nouvelles tâches de ce chat. Sécurité et détection de blocage restent actives."),
    "de": ("Ressourcenlimits", "Limits aktivieren", "Keine Ressourcenlimits", "Gilt für neue Aufgaben in diesem Chat. Sicherheits- und Stillstandsschutz bleiben aktiv."),
    "pt-BR": ("Limites de recursos", "Ativar limites", "Sem limites de recursos", "Aplica-se a novas tarefas deste chat. Segurança e detecção de bloqueio permanecem ativas."),
    "ru": ("Лимиты ресурсов", "Включить лимиты", "Без лимитов ресурсов", "Для новых задач в этом чате. Защита безопасности и обнаружение зависания остаются включены."),
    "ar": ("حدود موارد المهام", "تفعيل الحدود", "بلا حدود للموارد", "يسري على المهام الجديدة في هذه المحادثة. تبقى حماية الأمان والتوقف مفعلة."),
    "hi": ("कार्य संसाधन सीमाएँ", "सीमाएँ चालू करें", "संसाधन सीमा नहीं", "इस चैट के नए कार्यों पर लागू। सुरक्षा और रुकावट की निगरानी चालू रहती है।"),
    "id": ("Batas sumber daya tugas", "Aktifkan batas", "Tanpa batas sumber daya", "Berlaku untuk tugas baru di chat ini. Perlindungan keamanan dan deteksi macet tetap aktif."),
}


def limits_labels(locale: str) -> tuple[str, str, str, str]:
    return LABELS.get(locale, LABELS["en"])

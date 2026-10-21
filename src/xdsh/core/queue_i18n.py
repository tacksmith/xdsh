"""Localized queue choices, separate from task text and admission policy."""

from __future__ import annotations

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("interrupt", "merge", "queue", "replace", "drop", "queued", "suggestion", "notice")
_ROWS: dict[str, tuple[str, ...]] = {
    "en": (
        "Interrupt, then resume",
        "Merge into active task",
        "Keep normal queue",
        "Replace active task",
        "Discard this message",
        "Queued {id} · position {position}",
        "Suggested action: {action}",
        "Suggestion only. Interrupt: save progress, run the new task, then resume. Merge: stop the current execution segment and continue with the supplement now. Queue: FIFO. Replace: do not resume the old task. Completed tool actions are not undone.",
    ),
    "zh-TW": (
        "插隊並續接目前",
        "合併到目前任務",
        "保持正常排隊",
        "取代目前任務",
        "取消這條消息",
        "已排隊 {id} · 第 {position} 位",
        "關係判斷建議：{action}",
        "僅為建議。插隊會保存進度，先執行新任務再續接；合併會中止目前執行段，立即按原任務加補充續跑；正常排隊維持 FIFO；取代不恢復原任務。已發生的工具操作不會被撤回。",
    ),
    "zh-CN": (
        "插队并续接当前",
        "合并到当前任务",
        "保持正常排队",
        "取代当前任务",
        "取消这条消息",
        "已排队 {id} · 第 {position} 位",
        "关系判断建议：{action}",
        "仅为建议。插队会保存进度，先执行新任务再续接；合并会中止当前执行段，立即按原任务加补充续跑；正常排队保持 FIFO；取代不恢复原任务。已发生的工具操作不会被撤回。",
    ),
    "ja": (
        "割り込み後に再開",
        "現在のタスクに統合",
        "通常の順番待ち",
        "現在のタスクを置換",
        "このメッセージを破棄",
        "待機中 {id} · 順番 {position}",
        "推奨操作: {action}",
        "提案のみです。割り込みは進捗を保存し、新タスク後に再開。統合は実行区間を停止し補足付きで直ちに続行。待機は FIFO。置換では旧タスクを再開しません。実行済み操作は取り消されません。",
    ),
    "ko": (
        "끼어들기 후 재개",
        "현재 작업에 병합",
        "일반 대기 유지",
        "현재 작업 교체",
        "메시지 버리기",
        "대기 중 {id} · 순서 {position}",
        "권장 동작: {action}",
        "제안일 뿐입니다. 끼어들기는 진행을 저장하고 새 작업 후 재개합니다. 병합은 실행 구간을 멈추고 보충 내용과 함께 즉시 계속합니다. 대기는 FIFO입니다. 교체는 이전 작업을 재개하지 않습니다. 이미 수행한 작업은 취소되지 않습니다.",
    ),
    "es": (
        "Interrumpir y reanudar",
        "Fusionar con tarea activa",
        "Mantener cola normal",
        "Reemplazar tarea activa",
        "Descartar mensaje",
        "En cola {id} · posición {position}",
        "Acción sugerida: {action}",
        "Solo una sugerencia. Interrumpir guarda el progreso y reanuda tras la nueva tarea. Fusionar detiene el segmento actual y continúa con el suplemento. Cola: FIFO. Reemplazar no reanuda la tarea anterior. Las acciones ya realizadas no se deshacen.",
    ),
    "fr": (
        "Interrompre puis reprendre",
        "Fusionner avec la tâche",
        "Garder la file normale",
        "Remplacer la tâche",
        "Ignorer ce message",
        "En attente {id} · position {position}",
        "Action suggérée : {action}",
        "Suggestion uniquement. Interrompre conserve la progression et reprend après la nouvelle tâche. Fusionner arrête le segment courant et continue avec le complément. File : FIFO. Remplacer ne reprend pas l’ancienne tâche. Les actions déjà réalisées ne sont pas annulées.",
    ),
    "de": (
        "Unterbrechen, dann fortsetzen",
        "In laufende Aufgabe integrieren",
        "Normale Warteschlange",
        "Laufende Aufgabe ersetzen",
        "Nachricht verwerfen",
        "Eingereiht {id} · Position {position}",
        "Vorgeschlagene Aktion: {action}",
        "Nur ein Vorschlag. Unterbrechen sichert den Fortschritt und setzt nach der neuen Aufgabe fort. Integrieren stoppt den Abschnitt und fährt mit der Ergänzung fort. Warteschlange: FIFO. Ersetzen setzt die alte Aufgabe nicht fort. Ausgeführte Aktionen werden nicht rückgängig gemacht.",
    ),
    "pt-BR": (
        "Interromper e retomar",
        "Mesclar com tarefa ativa",
        "Manter fila normal",
        "Substituir tarefa ativa",
        "Descartar mensagem",
        "Na fila {id} · posição {position}",
        "Ação sugerida: {action}",
        "Apenas sugestão. Interromper salva o progresso e retoma após a nova tarefa. Mesclar interrompe o trecho atual e continua com o complemento. Fila: FIFO. Substituir não retoma a tarefa anterior. Ações já realizadas não são desfeitas.",
    ),
    "ru": (
        "Прервать, затем продолжить",
        "Дополнить текущую задачу",
        "Оставить в обычной очереди",
        "Заменить текущую задачу",
        "Отбросить сообщение",
        "В очереди {id} · позиция {position}",
        "Рекомендуемое действие: {action}",
        "Это лишь рекомендация. Прерывание сохраняет прогресс и возобновляет задачу после новой. Дополнение останавливает текущий сегмент и продолжает с новыми указаниями. Очередь: FIFO. Замена не возобновляет старую задачу. Выполненные действия не отменяются.",
    ),
    "ar": (
        "مقاطعة ثم استئناف",
        "دمج مع المهمة الحالية",
        "إبقاء الانتظار العادي",
        "استبدال المهمة الحالية",
        "تجاهل الرسالة",
        "في الانتظار {id} · الموضع {position}",
        "الإجراء المقترح: {action}",
        "مجرد اقتراح. المقاطعة تحفظ التقدم وتستأنف بعد المهمة الجديدة. الدمج يوقف الجزء الجاري ويتابع فوراً مع الإضافة. الانتظار حسب FIFO. الاستبدال لا يستأنف المهمة القديمة. الإجراءات المنفذة لا تُلغى.",
    ),
    "hi": (
        "बीच में चलाएँ, फिर जारी रखें",
        "वर्तमान कार्य में मिलाएँ",
        "सामान्य कतार रखें",
        "वर्तमान कार्य बदलें",
        "संदेश हटाएँ",
        "कतार में {id} · स्थान {position}",
        "सुझाया गया कार्य: {action}",
        "केवल सुझाव। बाधित करना प्रगति सहेजकर नए कार्य के बाद पुराना जारी करता है। मिलाना वर्तमान खंड रोककर पूरक निर्देश के साथ तुरंत जारी करता है। कतार: FIFO। बदलने पर पुराना कार्य फिर शुरू नहीं होता। हो चुके कार्य वापस नहीं होते।",
    ),
    "id": (
        "Sela lalu lanjutkan",
        "Gabungkan ke tugas aktif",
        "Tetap antre biasa",
        "Ganti tugas aktif",
        "Buang pesan ini",
        "Dalam antrean {id} · posisi {position}",
        "Tindakan yang disarankan: {action}",
        "Hanya saran. Menyela menyimpan progres dan melanjutkan setelah tugas baru. Menggabungkan menghentikan segmen aktif dan langsung melanjutkan dengan tambahan. Antrean: FIFO. Mengganti tidak melanjutkan tugas lama. Tindakan yang sudah dilakukan tidak dibatalkan.",
    ),
}


_ACK_KEYS = ("kept", "dropped", "promoted")
_ACK_ROWS: dict[str, tuple[str, ...]] = {
    "en": (
        "Queue retained {id} · position {position}; active task unchanged.",
        "Removed queued task {id}; it will not run.",
        "Queued task {id} is next.",
    ),
    "zh-TW": (
        "已保持正常排隊 {id} · 第 {position} 位; 目前任務不受影響。",
        "已移除排隊任務 {id}; 不會執行。",
        "排隊任務 {id} 已升為下一個。",
    ),
    "zh-CN": (
        "已保持正常排队 {id} · 第 {position} 位；当前任务不受影响。",
        "已移除排队任务 {id}；不会执行。",
        "排队任务 {id} 已升为下一个。",
    ),
    "ja": (
        "待機を維持 {id} · 順番 {position}。現在のタスクは変更しません。",
        "待機タスク {id} を削除しました。実行しません。",
        "待機タスク {id} を次に実行します。",
    ),
    "ko": (
        "대기 유지 {id} · 순서 {position}. 현재 작업은 변경되지 않습니다.",
        "대기 작업 {id}를 제거했습니다. 실행되지 않습니다.",
        "대기 작업 {id}가 다음입니다.",
    ),
    "es": (
        "Cola conservada {id} · posición {position}; tarea activa sin cambios.",
        "Tarea {id} retirada de la cola; no se ejecutará.",
        "La tarea {id} será la siguiente.",
    ),
    "fr": (
        "File conservée {id} · position {position} ; tâche active inchangée.",
        "Tâche {id} retirée de la file ; elle ne sera pas exécutée.",
        "La tâche {id} sera la prochaine.",
    ),
    "de": (
        "Warteschlange beibehalten {id} · Position {position}; aktive Aufgabe unverändert.",
        "Aufgabe {id} aus der Warteschlange entfernt; sie wird nicht ausgeführt.",
        "Aufgabe {id} ist als Nächstes dran.",
    ),
    "pt-BR": (
        "Fila mantida {id} · posição {position}; tarefa ativa inalterada.",
        "Tarefa {id} removida da fila; não será executada.",
        "A tarefa {id} será a próxima.",
    ),
    "ru": (
        "Очередь сохранена {id} · позиция {position}; текущая задача не изменена.",
        "Задача {id} удалена из очереди; она не будет выполнена.",
        "Задача {id} будет следующей.",
    ),
    "ar": (
        "تم إبقاء الانتظار {id} · الموضع {position}؛ المهمة الحالية لم تتغير.",
        "أُزيلت المهمة {id} من الانتظار؛ لن تُنفّذ.",
        "المهمة {id} هي التالية.",
    ),
    "hi": (
        "कतार कायम {id} · स्थान {position}; सक्रिय कार्य नहीं बदला।",
        "कार्य {id} कतार से हटा दिया गया; यह नहीं चलेगा।",
        "कार्य {id} अगला होगा।",
    ),
    "id": (
        "Antrean dipertahankan {id} · posisi {position}; tugas aktif tidak berubah.",
        "Tugas {id} dihapus dari antrean; tidak akan dijalankan.",
        "Tugas {id} berikutnya.",
    ),
}


_REASON_KEYS = ("replacement", "urgent", "supplement", "independent", "unclear")
_HANDOFF_KEYS = ("merge", "interrupt", "replace", "uncertain", "finished", "credentials")
_HANDOFF_ROWS = {
    "en": ("Merged {id}: progress retained; continuing the original task with the supplement now.", "Running {id} first; the previous task resumes afterwards. Progress retained.", "Previous task stopped; {id} is next. The previous task will not resume automatically.", "Handoff needs verification ({code}). No automatic replay; check /queue before retrying.", "The previous task already completed. New instruction stays queued; no duplicate resumption.", "The active task used credential approval. To avoid replaying secrets, merge/resume is unavailable. Choose queue, replace or discard."),
    "zh-TW": ("已合併 {id}: 保留進度, 立即按原任務+補充續跑, 不等待原任務完成。", "{id} 先執行, 完成後自動續接 A; 部分進度已保留。", "已停止原任務; {id} 已升為下一個, 不自動恢復 A。", "切換狀態需要核對 ({code})。不自動重放；重試前請查看 /queue。", "原任務已完成，新指令保留排隊，不重複續接。", "目前任務曾通過憑據確認。為避免重放秘密，不提供合併或插隊續接；可選排隊、取代或移除。"),
    "zh-CN": ("已合并 {id}：保留进度，立即按原任务加补充继续执行。", "优先执行 {id}，完成后自动续接原任务；进度已保留。", "原任务已停止，{id} 将下一个执行，不自动恢复原任务。", "切换状态需要核对 ({code})。不自动重放；重试前请查看 /queue。", "原任务已完成，新指令保留排队，不重复续接。", "当前任务曾通过凭据确认。为避免重放秘密，不提供合并或插队续接；可选排队、取代或移除。"),
    "ja": ("{id} を統合しました。進捗を保持し、元のタスクに補足を加えて直ちに続行します。", "{id} を先に実行し、その後元のタスクを再開します。進捗は保持済みです。", "元のタスクを停止しました。次は {id} です。元のタスクは自動再開しません。", "切り替え状態の確認が必要です ({code})。自動再実行しません。再試行前に /queue を確認してください。", "元のタスクは完了しました。新しい指示は待機中です。重複再開しません。", "現在のタスクは認証情報の承認を使用しました。秘密情報の再送を防ぐため統合・割り込み再開は使えません。待機・置換・破棄を選んでください。"),
    "ko": ("{id}를 병합했습니다. 진행을 보존하고 원래 작업에 보충 내용을 더해 즉시 계속합니다.", "{id}를 먼저 실행한 뒤 이전 작업을 재개합니다. 진행은 보존되었습니다.", "이전 작업을 중지했습니다. 다음은 {id}입니다. 이전 작업은 자동 재개되지 않습니다.", "전환 상태 확인이 필요합니다 ({code}). 자동 재실행하지 않습니다. 재시도 전에 /queue를 확인하세요.", "이전 작업이 이미 완료되었습니다. 새 지시는 대기하며 중복 재개하지 않습니다.", "현재 작업은 자격 증명 승인을 사용했습니다. 비밀 재전송 방지를 위해 병합과 끼어들기 후 재개는 사용할 수 없습니다. 대기, 교체, 삭제를 선택하세요."),
    "es": ("{id} fusionada: progreso conservado; se continúa ahora con el complemento.", "Se ejecuta primero {id}; después se reanuda la anterior. Progreso conservado.", "Tarea anterior detenida; sigue {id}. No se reanudará la anterior automáticamente.", "Verifica el cambio ({code}). Sin repetición automática; revisa /queue antes de reintentar.", "La tarea anterior ya terminó. La nueva sigue en cola, sin reanudación duplicada.", "La tarea activa usó aprobación de credenciales. Para no reenviar secretos, no se permite fusionar ni interrumpir y reanudar. Elige cola, reemplazo o descarte."),
    "fr": ("{id} fusionnée : progression conservée ; reprise immédiate avec le complément.", "{id} passe en premier, puis la tâche précédente reprend. Progression conservée.", "Tâche précédente arrêtée ; {id} est la prochaine. Pas de reprise automatique de l’ancienne.", "Vérifiez le transfert ({code}). Pas de répétition automatique ; consultez /queue avant de réessayer.", "La tâche précédente est terminée. La nouvelle reste en attente, sans reprise en double.", "La tâche active utilisait une approbation d’identifiants. Pour éviter de renvoyer des secrets, fusion et interruption avec reprise sont indisponibles. Choisissez file, remplacement ou abandon."),
    "de": ("{id} integriert: Fortschritt gespeichert; die ursprüngliche Aufgabe läuft mit Ergänzung weiter.", "Zuerst {id}, danach wird die vorherige Aufgabe fortgesetzt. Fortschritt gespeichert.", "Vorherige Aufgabe gestoppt; als Nächstes {id}. Keine automatische Fortsetzung der alten Aufgabe.", "Übergabe prüfen ({code}). Keine automatische Wiederholung; vor erneutem Versuch /queue prüfen.", "Vorherige Aufgabe bereits abgeschlossen. Neue Anweisung bleibt eingereiht; keine doppelte Fortsetzung.", "Die aktive Aufgabe nutzte eine Zugangsdatenfreigabe. Zum Schutz vor erneuter Geheimnisübertragung sind Integration und Unterbrechen mit Fortsetzen gesperrt. Warteschlange, Ersetzen oder Verwerfen wählen."),
    "pt-BR": ("{id} mesclada: progresso preservado; continuando agora com o complemento.", "Executando {id} primeiro; a tarefa anterior será retomada depois. Progresso preservado.", "Tarefa anterior parada; {id} é a próxima. A anterior não será retomada automaticamente.", "Verifique a transição ({code}). Sem repetição automática; veja /queue antes de tentar novamente.", "A tarefa anterior já terminou. A nova continua na fila, sem retomada duplicada.", "A tarefa ativa usou aprovação de credenciais. Para evitar reenviar segredos, mesclar e interromper com retomada estão indisponíveis. Escolha fila, substituição ou descarte."),
    "ru": ("{id} объединена: прогресс сохранён; исходная задача продолжается с дополнением.", "Сначала выполняется {id}, затем возобновится предыдущая задача. Прогресс сохранён.", "Предыдущая задача остановлена; следующая — {id}. Старая задача не возобновится автоматически.", "Проверьте переключение ({code}). Автоматического повтора нет; перед повтором проверьте /queue.", "Предыдущая задача уже завершена. Новая остаётся в очереди, без повторного возобновления.", "Текущая задача использовала подтверждённые учётные данные. Чтобы не пересылать секреты повторно, объединение и прерывание с возобновлением недоступны. Выберите очередь, замену или удаление."),
    "ar": ("دُمجت {id}: حُفظ التقدم وتستمر المهمة الأصلية مع الإضافة الآن.", "تُنفّذ {id} أولًا ثم تُستأنف المهمة السابقة. حُفظ التقدم.", "توقفت المهمة السابقة؛ التالية {id}. لن تُستأنف السابقة تلقائيًا.", "تحقّق من حالة الانتقال ({code}). لا إعادة تلقائية؛ افحص /queue قبل المحاولة مجددًا.", "اكتملت المهمة السابقة. تبقى التعليمات الجديدة في الانتظار دون استئناف مكرر.", "استخدمت المهمة الحالية موافقة بيانات اعتماد. لمنع إعادة إرسال الأسرار، الدمج والمقاطعة مع الاستئناف غير متاحين. اختر الانتظار أو الاستبدال أو الحذف."),
    "hi": ("{id} मिलाया गया: प्रगति सुरक्षित है; मूल कार्य पूरक जानकारी के साथ अभी जारी है।", "पहले {id} चलेगा, फिर पिछला कार्य जारी होगा। प्रगति सुरक्षित है।", "पिछला कार्य रोक दिया गया; अगला {id} है। पिछला कार्य अपने आप फिर शुरू नहीं होगा।", "बदलाव की स्थिति जाँचें ({code})। स्वतः दोहराव नहीं होगा; फिर कोशिश करने से पहले /queue देखें।", "पिछला कार्य पूरा हो चुका है। नया निर्देश कतार में रहेगा; दोबारा जारी नहीं होगा।", "सक्रिय कार्य ने क्रेडेंशियल स्वीकृति इस्तेमाल की थी। रहस्य फिर भेजने से बचने के लिए मिलाना और बीच में चलाकर फिर जारी करना उपलब्ध नहीं है। कतार, बदलना या हटाना चुनें।"),
    "id": ("{id} digabung: progres tersimpan; tugas asli langsung dilanjutkan dengan tambahan.", "Menjalankan {id} dahulu, lalu melanjutkan tugas sebelumnya. Progres tersimpan.", "Tugas sebelumnya dihentikan; berikutnya {id}. Tugas lama tidak dilanjutkan otomatis.", "Periksa status peralihan ({code}). Tidak diulang otomatis; periksa /queue sebelum mencoba lagi.", "Tugas sebelumnya sudah selesai. Instruksi baru tetap mengantre, tanpa pelanjutan ganda.", "Tugas aktif memakai persetujuan kredensial. Untuk mencegah pengiriman ulang rahasia, penggabungan dan penyelaan dengan pelanjutan tidak tersedia. Pilih antre, ganti, atau buang."),
}
_REASON_ROWS = {
    "en": ("Wording suggests replacing or cancelling the active task.", "Wording suggests priority handling.", "Follow-up wording or shared terms suggest a supplement to the active task.", "No clear supplement, replacement or urgency signal was found; keep FIFO.", "Not enough information to judge the relationship; keep FIFO."),
    "zh-TW": ("措辭可能表示取代或取消目前任務。", "措辭可能表示需要優先處理。", "補充措辭或重疊詞彙可能表示與目前任務有關。", "未發現明確補充、取代或緊急訊號，建議保持 FIFO。", "資訊不足以判斷任務關係，建議保持 FIFO。"),
    "zh-CN": ("措辞可能表示取代或取消当前任务。", "措辞可能表示需要优先处理。", "补充措辞或重叠词汇可能表示与当前任务有关。", "未发现明确补充、取代或紧急信号，建议保持 FIFO。", "信息不足以判断任务关系，建议保持 FIFO。"),
    "ja": ("表現から現在のタスクの置換・取消が示唆されます。", "表現から優先処理が示唆されます。", "補足表現や共通語から現在のタスクへの補足が示唆されます。", "補足・置換・緊急の明確な兆候がないため FIFO を推奨します。", "関係を判断する情報が不足しているため FIFO を推奨します。"),
    "ko": ("표현상 현재 작업의 교체나 취소를 뜻할 수 있습니다.", "표현상 우선 처리가 필요할 수 있습니다.", "후속 표현이나 공통 단어로 보아 현재 작업의 보충일 수 있습니다.", "보충, 교체, 긴급 신호가 명확하지 않아 FIFO를 권장합니다.", "관계를 판단할 정보가 부족하여 FIFO를 권장합니다."),
    "es": ("La redacción sugiere reemplazar o cancelar la tarea activa.", "La redacción sugiere atención prioritaria.", "La continuación o los términos compartidos sugieren un complemento.", "No hay señales claras de complemento, reemplazo o urgencia; mantener FIFO.", "No hay información suficiente para relacionar las tareas; mantener FIFO."),
    "fr": ("La formulation suggère de remplacer ou d’annuler la tâche active.", "La formulation suggère un traitement prioritaire.", "Le suivi ou les termes communs suggèrent un complément à la tâche active.", "Aucun signal clair de complément, remplacement ou urgence ; garder FIFO.", "Informations insuffisantes pour déterminer le lien ; garder FIFO."),
    "de": ("Die Formulierung deutet auf Ersetzen oder Abbrechen der aktiven Aufgabe hin.", "Die Formulierung deutet auf vorrangige Bearbeitung hin.", "Ergänzende Formulierungen oder gemeinsame Begriffe deuten auf einen Nachtrag hin.", "Kein klares Ergänzungs-, Ersatz- oder Dringlichkeitssignal; FIFO beibehalten.", "Zu wenig Informationen über den Zusammenhang; FIFO beibehalten."),
    "pt-BR": ("A redação sugere substituir ou cancelar a tarefa ativa.", "A redação sugere tratamento prioritário.", "A continuação ou termos em comum sugerem um complemento à tarefa ativa.", "Sem sinais claros de complemento, substituição ou urgência; manter FIFO.", "Informação insuficiente para relacionar as tarefas; manter FIFO."),
    "ru": ("Формулировка указывает на возможную замену или отмену текущей задачи.", "Формулировка указывает на возможную срочность.", "Дополнение или общие слова указывают на возможную связь с текущей задачей.", "Нет явных признаков дополнения, замены или срочности; рекомендуется FIFO.", "Недостаточно данных для определения связи; рекомендуется FIFO."),
    "ar": ("قد تشير الصياغة إلى استبدال المهمة الحالية أو إلغائها.", "قد تشير الصياغة إلى الحاجة للأولوية.", "قد تشير صياغة المتابعة أو الكلمات المشتركة إلى إضافة للمهمة الحالية.", "لا توجد إشارة واضحة لإضافة أو استبدال أو استعجال؛ يُنصح بـ FIFO.", "المعلومات غير كافية لتحديد العلاقة؛ يُنصح بـ FIFO."),
    "hi": ("शब्दों से सक्रिय कार्य बदलने या रद्द करने का संकेत मिलता है।", "शब्दों से प्राथमिकता देने का संकेत मिलता है।", "आगे की बात या साझा शब्द सक्रिय कार्य में पूरक जानकारी का संकेत देते हैं।", "पूरक, बदलाव या तात्कालिकता का स्पष्ट संकेत नहीं; FIFO रखें।", "संबंध तय करने के लिए जानकारी कम है; FIFO रखें।"),
    "id": ("Susunan kata mengisyaratkan penggantian atau pembatalan tugas aktif.", "Susunan kata mengisyaratkan penanganan prioritas.", "Kata lanjutan atau istilah bersama mengisyaratkan tambahan pada tugas aktif.", "Tidak ada sinyal jelas tambahan, penggantian, atau urgensi; pertahankan FIFO.", "Informasi hubungan tugas belum cukup; pertahankan FIFO."),
}


def queue_text(locale: str, key: str, **values: object) -> str:
    if key.startswith("handoff.") and key[8:] in _HANDOFF_KEYS:
        return _HANDOFF_ROWS[canonical_language(locale)][_HANDOFF_KEYS.index(key[8:])].format(**values)
    if key.startswith("reason.") and key[7:] in _REASON_KEYS:
        return _REASON_ROWS[canonical_language(locale)][_REASON_KEYS.index(key[7:])]
    if key in _ACK_KEYS:
        return _ACK_ROWS[canonical_language(locale)][_ACK_KEYS.index(key)].format(**values)
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index].format(**values)


def validate_queue_catalog() -> None:
    from string import Formatter

    if set(_HANDOFF_ROWS) != set(language_codes()):
        raise ValueError("queue handoff locale mismatch")
    for handoff_row in _HANDOFF_ROWS.values():
        if len(handoff_row) != len(_HANDOFF_KEYS):
            raise ValueError("queue handoff key mismatch")
        for message, english in zip(handoff_row, _HANDOFF_ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message.strip() or fields != expected:
                raise ValueError("queue handoff placeholder mismatch")

    if set(_REASON_ROWS) != set(language_codes()):
        raise ValueError("queue reason locale mismatch")
    for reason_row in _REASON_ROWS.values():
        if len(reason_row) != len(_REASON_KEYS) or any(not message.strip() for message in reason_row):
            raise ValueError("queue reason key mismatch")

    if set(_ROWS) != set(language_codes()):
        raise ValueError("queue locale coverage mismatch")
    if set(_ACK_ROWS) != set(language_codes()):
        raise ValueError("queue acknowledgement locale mismatch")
    for row in _ACK_ROWS.values():
        if len(row) != len(_ACK_KEYS):
            raise ValueError("queue acknowledgement key mismatch")
        for message, english in zip(row, _ACK_ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message.strip() or fields != expected:
                raise ValueError("queue acknowledgement placeholder mismatch")
    for row in _ROWS.values():
        if len(row) != len(_KEYS):
            raise ValueError("queue key coverage mismatch")
        for message, english in zip(row, _ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message.strip() or fields != expected:
                raise ValueError("queue placeholder mismatch")

"""Deterministic, credential-free localization shared by XDSH interfaces.

The catalog deliberately contains only product-owned text.  Model output and
provider payloads are never sent to a translation service.  A missing optional
translation falls back to English so an interface never silently mixes in the
historical Chinese default.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One selectable interface language."""

    code: str
    native_name: str
    english_name: str
    telegram_code: str
    direction: str = "ltr"


SUPPORTED_LANGUAGES: Final[tuple[LanguageSpec, ...]] = (
    LanguageSpec("zh-TW", "繁體中文", "Traditional Chinese", "zh"),
    LanguageSpec("zh-CN", "简体中文", "Simplified Chinese", "zh"),
    LanguageSpec("en", "English", "English", "en"),
    LanguageSpec("ja", "日本語", "Japanese", "ja"),
    LanguageSpec("ko", "한국어", "Korean", "ko"),
    LanguageSpec("es", "Español", "Spanish", "es"),
    LanguageSpec("fr", "Français", "French", "fr"),
    LanguageSpec("de", "Deutsch", "German", "de"),
    LanguageSpec("pt-BR", "Português", "Portuguese", "pt"),
    LanguageSpec("ru", "Русский", "Russian", "ru"),
    LanguageSpec("ar", "العربية", "Arabic", "ar", direction="rtl"),
    LanguageSpec("hi", "हिन्दी", "Hindi", "hi"),
    LanguageSpec("id", "Bahasa Indonesia", "Indonesian", "id"),
)

DEFAULT_LANGUAGE: Final = "en"
UPGRADE_DEFAULT_LANGUAGE: Final = "zh-TW"
_BY_CODE = MappingProxyType({item.code: item for item in SUPPORTED_LANGUAGES})
_CODE_CASEFOLD = MappingProxyType({item.code.casefold(): item.code for item in SUPPORTED_LANGUAGES})
_ALIASES = MappingProxyType(
    {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "zh-hans": "zh-CN",
        "zh-sg": "zh-CN",
        "简体中文": "zh-CN",
        "簡體中文": "zh-CN",
        "zh-tw": "zh-TW",
        "zh-hant": "zh-TW",
        "zh-hk": "zh-TW",
        "zh-mo": "zh-TW",
        "繁體中文": "zh-TW",
        "繁体中文": "zh-TW",
        "en-us": "en",
        "en-gb": "en",
        "english": "en",
        "日本語": "ja",
        "jp": "ja",
        "한국어": "ko",
        "kr": "ko",
        "español": "es",
        "français": "fr",
        "português": "pt-BR",
        "pt": "pt-BR",
        "pt-br": "pt-BR",
        "русский": "ru",
        "العربية": "ar",
        "हिन्दी": "hi",
        "hindi": "hi",
        "bahasa indonesia": "id",
        "in": "id",
    }
)
_SAFE_KEY = re.compile(r"^[a-z0-9_.-]{1,96}$")


def canonical_language(value: str) -> str:
    """Return a supported canonical code or raise ``ValueError``."""

    normalized = value.strip().replace("_", "-")
    if not normalized:
        raise ValueError("language must be non-empty")
    direct = _CODE_CASEFOLD.get(normalized.casefold())
    if direct is not None:
        return direct
    alias = _ALIASES.get(normalized.casefold())
    if alias is not None:
        return alias
    raise ValueError("unsupported language")


def language_spec(value: str) -> LanguageSpec:
    """Resolve one canonical or aliased language value."""

    return _BY_CODE[canonical_language(value)]


def language_codes() -> tuple[str, ...]:
    return tuple(item.code for item in SUPPORTED_LANGUAGES)


_COMMAND_NAMES: Final = (
    "start",
    "help",
    "status",
    "language",
    "queue",
    "background",
    "jobs",
    "cron",
    "cancel",
    "new",
    "session",
    "search",
    "clear",
    "model",
    "reasoning",
    "thinking",
    "learn",
    "web",
    "mcp",
    "browser",
    "media",
    "plugins",
    "compact",
    "feedback",
    "goal",
    "plan",
    "permission",
    "export",
    "profile",
    "memory",
    "fallback",
    "skills",
    "cost",
    "context",
    "doctor",
    "init",
    "review",
)


_COMMAND_ROWS: Final[dict[str, tuple[str, ...]]] = {
    "en": (
        "Start XDSH and show usage",
        "Show commands and task help",
        "Show current task status",
        "Choose interface and response language",
        "Manage queue, priority and follow-ups",
        "Create a durable background task",
        "View and control the durable job board",
        "Manage one-time, interval and Cron schedules",
        "Cancel the active task",
        "Create and switch to a new session",
        "Manage and switch sessions",
        "Search current or all session history",
        "Clear the current DSH context",
        "Select a session model by provider",
        "Set LLM reasoning effort",
        "Show or hide structured thinking",
        "Manage approval-based learning",
        "Fetch allowlisted web text",
        "List or call approved MCP tools",
        "Use the restricted browser gateway",
        "Show native-safe media limits",
        "Discover and manage DSH plugins",
        "Compact conversation context",
        "Record feedback for this DSH session",
        "Set or view the long-task goal",
        "Enter or leave plan mode",
        "Show sandbox and approval policy",
        "Export the current session",
        "Switch or create a work profile",
        "Show memory and retrieval status",
        "View or set the fallback model",
        "View built-in and project Skills",
        "Show session usage and cost",
        "Show context-window usage",
        "Run XDSH environment checks",
        "Generate project agent instructions",
        "Run a code review",
    ),
    "zh-TW": (
        "啟動 XDSH 並顯示使用方法",
        "顯示命令與任務說明",
        "查看目前任務狀態",
        "選擇介面與回答語言",
        "管理排隊、插隊與補充任務",
        "建立持久背景任務",
        "查看與控制持久任務看板",
        "管理一次、間隔與 Cron 排程",
        "中斷目前執行中的任務",
        "建立並切換到新會話",
        "管理與切換多個會話",
        "搜尋目前或全部會話歷史",
        "清空目前 DSH 上下文",
        "按 Provider 選擇會話模型",
        "調整 LLM 推理強度",
        "顯示或隱藏結構化 thinking",
        "管理需批准的自主學習",
        "讀取 allowlist 允許的網頁文字",
        "列出或呼叫已核准的 MCP 工具",
        "使用受限 browser gateway",
        "查看 native-safe 媒體限制",
        "發現與管理 DSH 插件",
        "壓縮對話上下文",
        "記錄本次 DSH 會話意見",
        "設定或查看長任務目標",
        "進入或離開計畫模式",
        "查看沙箱與核准策略",
        "匯出目前會話",
        "切換或建立工作 profile",
        "查看記憶與檢索狀態",
        "查看或設定備用模型",
        "查看內置與專案 Skills",
        "查看會話用量與花費",
        "查看上下文視窗占用",
        "執行 XDSH 環境自檢",
        "產生專案代理說明檔",
        "執行程式碼審查",
    ),
    "zh-CN": (
        "启动 XDSH 并显示使用方法",
        "显示命令与任务说明",
        "查看当前任务状态",
        "选择界面与回答语言",
        "管理排队、插队与补充任务",
        "创建持久后台任务",
        "查看与控制持久任务看板",
        "管理单次、间隔与 Cron 计划",
        "中断当前执行中的任务",
        "创建并切换到新会话",
        "管理与切换多个会话",
        "搜索当前或全部会话历史",
        "清空当前 DSH 上下文",
        "按 Provider 选择会话模型",
        "调整 LLM 推理强度",
        "显示或隐藏结构化 thinking",
        "管理需批准的自主学习",
        "读取 allowlist 允许的网页文本",
        "列出或调用已批准的 MCP 工具",
        "使用受限 browser gateway",
        "查看 native-safe 媒体限制",
        "发现与管理 DSH 插件",
        "压缩对话上下文",
        "记录本次 DSH 会话意见",
        "设置或查看长任务目标",
        "进入或退出计划模式",
        "查看沙箱与审批策略",
        "导出当前会话",
        "切换或创建工作 profile",
        "查看记忆与检索状态",
        "查看或设置备用模型",
        "查看内置与项目 Skills",
        "查看会话用量与花费",
        "查看上下文窗口占用",
        "执行 XDSH 环境自检",
        "生成项目代理说明文件",
        "执行代码审查",
    ),
    "ja": (
        "XDSH を開始して使い方を表示",
        "コマンドとタスクのヘルプを表示",
        "現在のタスク状態を表示",
        "画面と回答の言語を選択",
        "キュー、優先実行、追加依頼を管理",
        "永続バックグラウンドタスクを作成",
        "永続ジョブボードを表示・操作",
        "単発・間隔・Cron スケジュールを管理",
        "実行中のタスクをキャンセル",
        "新しいセッションを作成して切替",
        "複数セッションを管理・切替",
        "現在または全セッション履歴を検索",
        "現在の DSH コンテキストを消去",
        "Provider 別にモデルを選択",
        "LLM の推論強度を設定",
        "構造化 thinking の表示を切替",
        "承認制の学習を管理",
        "許可済み Web テキストを取得",
        "承認済み MCP ツールを一覧・実行",
        "制限付き browser gateway を使用",
        "安全なメディア制限を表示",
        "DSH プラグインを検出・管理",
        "会話コンテキストを圧縮",
        "この DSH セッションの意見を記録",
        "長時間タスクの目標を設定・表示",
        "計画モードを開始・終了",
        "サンドボックスと承認方針を表示",
        "現在のセッションをエクスポート",
        "作業 profile を切替・作成",
        "メモリと検索状態を表示",
        "フォールバックモデルを表示・設定",
        "組み込み・プロジェクト Skills を表示",
        "セッション使用量と費用を表示",
        "コンテキスト使用量を表示",
        "XDSH 環境診断を実行",
        "プロジェクト用エージェント説明を生成",
        "コードレビューを実行",
    ),
    "ko": (
        "XDSH를 시작하고 사용법 표시",
        "명령과 작업 도움말 표시",
        "현재 작업 상태 표시",
        "인터페이스와 답변 언어 선택",
        "대기열, 우선 실행, 후속 작업 관리",
        "지속 백그라운드 작업 생성",
        "지속 작업 보드 보기 및 제어",
        "일회성·간격·Cron 일정 관리",
        "실행 중인 작업 취소",
        "새 세션 생성 및 전환",
        "여러 세션 관리 및 전환",
        "현재 또는 모든 세션 기록 검색",
        "현재 DSH 컨텍스트 지우기",
        "Provider별 세션 모델 선택",
        "LLM 추론 강도 설정",
        "구조화 thinking 표시 전환",
        "승인 기반 학습 관리",
        "허용된 웹 텍스트 가져오기",
        "승인된 MCP 도구 목록 및 호출",
        "제한된 browser gateway 사용",
        "안전한 미디어 제한 표시",
        "DSH 플러그인 검색 및 관리",
        "대화 컨텍스트 압축",
        "현재 DSH 세션 피드백 기록",
        "장기 작업 목표 설정 또는 보기",
        "계획 모드 시작 또는 종료",
        "샌드박스와 승인 정책 표시",
        "현재 세션 내보내기",
        "작업 profile 전환 또는 생성",
        "메모리와 검색 상태 표시",
        "대체 모델 보기 또는 설정",
        "내장 및 프로젝트 Skills 표시",
        "세션 사용량과 비용 표시",
        "컨텍스트 창 사용량 표시",
        "XDSH 환경 검사 실행",
        "프로젝트 에이전트 지침 생성",
        "코드 리뷰 실행",
    ),
    "es": (
        "Iniciar XDSH y mostrar el uso",
        "Mostrar comandos y ayuda de tareas",
        "Mostrar el estado de la tarea actual",
        "Elegir idioma de interfaz y respuestas",
        "Gestionar cola, prioridad y seguimientos",
        "Crear una tarea duradera en segundo plano",
        "Ver y controlar el panel de trabajos",
        "Gestionar horarios únicos, periódicos y Cron",
        "Cancelar la tarea activa",
        "Crear y cambiar a una sesión nueva",
        "Gestionar y cambiar sesiones",
        "Buscar el historial de una o todas las sesiones",
        "Borrar el contexto DSH actual",
        "Elegir modelo de sesión por proveedor",
        "Ajustar el nivel de razonamiento del LLM",
        "Mostrar u ocultar thinking estructurado",
        "Gestionar aprendizaje con aprobación",
        "Obtener texto web permitido",
        "Listar o llamar herramientas MCP aprobadas",
        "Usar el browser gateway restringido",
        "Mostrar límites seguros de medios",
        "Descubrir y gestionar plugins DSH",
        "Compactar el contexto de conversación",
        "Registrar comentarios de esta sesión DSH",
        "Definir o ver el objetivo de tarea larga",
        "Entrar o salir del modo de planificación",
        "Mostrar políticas de sandbox y aprobación",
        "Exportar la sesión actual",
        "Cambiar o crear un profile de trabajo",
        "Mostrar estado de memoria y búsqueda",
        "Ver o definir el modelo alternativo",
        "Ver Skills integradas y del proyecto",
        "Mostrar uso y coste de la sesión",
        "Mostrar uso de la ventana de contexto",
        "Ejecutar diagnósticos del entorno XDSH",
        "Generar instrucciones de agente del proyecto",
        "Ejecutar una revisión de código",
    ),
    "fr": (
        "Démarrer XDSH et afficher l’utilisation",
        "Afficher les commandes et l’aide des tâches",
        "Afficher l’état de la tâche actuelle",
        "Choisir la langue de l’interface et des réponses",
        "Gérer la file, la priorité et les suivis",
        "Créer une tâche durable en arrière-plan",
        "Afficher et contrôler le tableau des tâches",
        "Gérer les planifications uniques, périodiques et Cron",
        "Annuler la tâche active",
        "Créer et ouvrir une nouvelle session",
        "Gérer et changer de session",
        "Rechercher dans une ou toutes les sessions",
        "Effacer le contexte DSH actuel",
        "Choisir le modèle de session par fournisseur",
        "Régler l’intensité de raisonnement du LLM",
        "Afficher ou masquer le thinking structuré",
        "Gérer l’apprentissage avec approbation",
        "Lire du texte Web autorisé",
        "Lister ou appeler les outils MCP approuvés",
        "Utiliser le browser gateway restreint",
        "Afficher les limites média sécurisées",
        "Découvrir et gérer les plugins DSH",
        "Compacter le contexte de conversation",
        "Enregistrer un avis sur cette session DSH",
        "Définir ou voir l’objectif de tâche longue",
        "Entrer ou quitter le mode planification",
        "Afficher les politiques de sandbox et d’approbation",
        "Exporter la session actuelle",
        "Changer ou créer un profile de travail",
        "Afficher l’état de la mémoire et de la recherche",
        "Voir ou définir le modèle de secours",
        "Afficher les Skills intégrées et du projet",
        "Afficher l’usage et le coût de la session",
        "Afficher l’utilisation du contexte",
        "Exécuter le diagnostic de l’environnement XDSH",
        "Générer les instructions d’agent du projet",
        "Exécuter une revue de code",
    ),
    "de": (
        "XDSH starten und Nutzung anzeigen",
        "Befehle und Aufgabenhilfe anzeigen",
        "Status der aktuellen Aufgabe anzeigen",
        "Sprache für Oberfläche und Antworten wählen",
        "Warteschlange, Priorität und Ergänzungen verwalten",
        "Dauerhafte Hintergrundaufgabe erstellen",
        "Dauerhafte Aufgabenübersicht anzeigen und steuern",
        "Einmalige, Intervall- und Cron-Pläne verwalten",
        "Aktive Aufgabe abbrechen",
        "Neue Sitzung erstellen und wechseln",
        "Sitzungen verwalten und wechseln",
        "Aktuelle oder alle Sitzungsverläufe durchsuchen",
        "Aktuellen DSH-Kontext leeren",
        "Sitzungsmodell nach Anbieter wählen",
        "LLM-Denkintensität einstellen",
        "Strukturiertes thinking ein- oder ausblenden",
        "Genehmigungsbasiertes Lernen verwalten",
        "Freigegebenen Webtext abrufen",
        "Freigegebene MCP-Tools auflisten oder aufrufen",
        "Eingeschränktes browser gateway verwenden",
        "Sichere Mediengrenzen anzeigen",
        "DSH-Plugins erkennen und verwalten",
        "Gesprächskontext komprimieren",
        "Feedback zu dieser DSH-Sitzung speichern",
        "Langzeitziel festlegen oder anzeigen",
        "Planungsmodus starten oder beenden",
        "Sandbox- und Genehmigungsrichtlinie anzeigen",
        "Aktuelle Sitzung exportieren",
        "Arbeits-profile wechseln oder erstellen",
        "Speicher- und Suchstatus anzeigen",
        "Ersatzmodell anzeigen oder festlegen",
        "Integrierte und Projekt-Skills anzeigen",
        "Sitzungsnutzung und Kosten anzeigen",
        "Kontextfenster-Nutzung anzeigen",
        "XDSH-Umgebung prüfen",
        "Projekt-Agentenanweisungen erzeugen",
        "Code-Review ausführen",
    ),
    "pt-BR": (
        "Iniciar o XDSH e mostrar como usar",
        "Mostrar comandos e ajuda de tarefas",
        "Mostrar o estado da tarefa atual",
        "Escolher idioma da interface e das respostas",
        "Gerenciar fila, prioridade e complementos",
        "Criar tarefa persistente em segundo plano",
        "Ver e controlar o painel de tarefas",
        "Gerenciar agendas únicas, periódicas e Cron",
        "Cancelar a tarefa ativa",
        "Criar e alternar para uma nova sessão",
        "Gerenciar e alternar sessões",
        "Pesquisar o histórico de uma ou todas as sessões",
        "Limpar o contexto DSH atual",
        "Escolher modelo da sessão por provedor",
        "Ajustar o nível de raciocínio do LLM",
        "Mostrar ou ocultar thinking estruturado",
        "Gerenciar aprendizado com aprovação",
        "Obter texto web permitido",
        "Listar ou chamar ferramentas MCP aprovadas",
        "Usar o browser gateway restrito",
        "Mostrar limites seguros de mídia",
        "Descobrir e gerenciar plugins DSH",
        "Compactar o contexto da conversa",
        "Registrar feedback desta sessão DSH",
        "Definir ou ver o objetivo da tarefa longa",
        "Entrar ou sair do modo de planejamento",
        "Mostrar políticas de sandbox e aprovação",
        "Exportar a sessão atual",
        "Alternar ou criar um profile de trabalho",
        "Mostrar estado da memória e da busca",
        "Ver ou definir o modelo alternativo",
        "Ver Skills internas e do projeto",
        "Mostrar uso e custo da sessão",
        "Mostrar uso da janela de contexto",
        "Executar diagnóstico do ambiente XDSH",
        "Gerar instruções do agente do projeto",
        "Executar revisão de código",
    ),
    "ru": (
        "Запустить XDSH и показать справку",
        "Показать команды и справку по задачам",
        "Показать состояние текущей задачи",
        "Выбрать язык интерфейса и ответов",
        "Управлять очередью, приоритетом и дополнениями",
        "Создать постоянную фоновую задачу",
        "Просматривать и управлять доской задач",
        "Управлять разовыми, интервальными и Cron-запусками",
        "Отменить активную задачу",
        "Создать новую сессию и перейти в неё",
        "Управлять сессиями и переключаться между ними",
        "Искать в текущей или всех сессиях",
        "Очистить текущий контекст DSH",
        "Выбрать модель сессии по провайдеру",
        "Настроить уровень рассуждения LLM",
        "Показать или скрыть структурированное thinking",
        "Управлять обучением с подтверждением",
        "Получить разрешённый веб-текст",
        "Показать или вызвать одобренные MCP-инструменты",
        "Использовать ограниченный browser gateway",
        "Показать безопасные ограничения медиа",
        "Находить и управлять плагинами DSH",
        "Сжать контекст разговора",
        "Записать отзыв о текущей сессии DSH",
        "Задать или показать цель долгой задачи",
        "Войти или выйти из режима плана",
        "Показать правила sandbox и подтверждений",
        "Экспортировать текущую сессию",
        "Переключить или создать рабочий profile",
        "Показать состояние памяти и поиска",
        "Показать или задать резервную модель",
        "Показать встроенные и проектные Skills",
        "Показать использование и стоимость сессии",
        "Показать заполнение контекстного окна",
        "Запустить диагностику среды XDSH",
        "Создать инструкции агента проекта",
        "Запустить проверку кода",
    ),
    "ar": (
        "بدء XDSH وعرض طريقة الاستخدام",
        "عرض الأوامر ومساعدة المهام",
        "عرض حالة المهمة الحالية",
        "اختيار لغة الواجهة والردود",
        "إدارة الطابور والأولوية والمتابعات",
        "إنشاء مهمة خلفية دائمة",
        "عرض لوحة المهام الدائمة والتحكم بها",
        "إدارة الجداول الفردية والدورية وCron",
        "إلغاء المهمة النشطة",
        "إنشاء جلسة جديدة والانتقال إليها",
        "إدارة الجلسات والتبديل بينها",
        "البحث في سجل الجلسة الحالية أو جميع الجلسات",
        "مسح سياق DSH الحالي",
        "اختيار نموذج الجلسة حسب المزوّد",
        "ضبط مستوى استدلال LLM",
        "إظهار أو إخفاء thinking المنظّم",
        "إدارة التعلّم القائم على الموافقة",
        "جلب نص الويب المسموح",
        "عرض أو استدعاء أدوات MCP المعتمدة",
        "استخدام browser gateway المقيّد",
        "عرض حدود الوسائط الآمنة",
        "اكتشاف إضافات DSH وإدارتها",
        "ضغط سياق المحادثة",
        "تسجيل ملاحظات جلسة DSH الحالية",
        "ضبط أو عرض هدف المهمة الطويلة",
        "الدخول إلى وضع التخطيط أو الخروج منه",
        "عرض سياسة sandbox والموافقات",
        "تصدير الجلسة الحالية",
        "تبديل أو إنشاء profile عمل",
        "عرض حالة الذاكرة والاسترجاع",
        "عرض أو ضبط النموذج الاحتياطي",
        "عرض Skills المدمجة والخاصة بالمشروع",
        "عرض استخدام الجلسة وتكلفتها",
        "عرض استخدام نافذة السياق",
        "تشغيل فحوص بيئة XDSH",
        "إنشاء تعليمات وكيل المشروع",
        "تشغيل مراجعة الشيفرة",
    ),
    "hi": (
        "XDSH शुरू करें और उपयोग दिखाएँ",
        "कमांड और कार्य सहायता दिखाएँ",
        "वर्तमान कार्य की स्थिति दिखाएँ",
        "इंटरफ़ेस और उत्तर की भाषा चुनें",
        "कतार, प्राथमिकता और फॉलो-अप प्रबंधित करें",
        "स्थायी पृष्ठभूमि कार्य बनाएँ",
        "स्थायी कार्य बोर्ड देखें और नियंत्रित करें",
        "एकबारगी, अंतराल और Cron शेड्यूल प्रबंधित करें",
        "सक्रिय कार्य रद्द करें",
        "नया सत्र बनाएँ और उस पर जाएँ",
        "सत्र प्रबंधित करें और बदलें",
        "वर्तमान या सभी सत्र इतिहास खोजें",
        "वर्तमान DSH संदर्भ साफ़ करें",
        "प्रदाता के अनुसार सत्र मॉडल चुनें",
        "LLM reasoning स्तर सेट करें",
        "संरचित thinking दिखाएँ या छिपाएँ",
        "अनुमोदन-आधारित सीख प्रबंधित करें",
        "अनुमत वेब पाठ प्राप्त करें",
        "स्वीकृत MCP टूल सूचीबद्ध या कॉल करें",
        "सीमित browser gateway उपयोग करें",
        "सुरक्षित मीडिया सीमाएँ दिखाएँ",
        "DSH प्लगइन खोजें और प्रबंधित करें",
        "वार्तालाप संदर्भ compact करें",
        "इस DSH सत्र की प्रतिक्रिया दर्ज करें",
        "लंबे कार्य का लक्ष्य सेट करें या देखें",
        "योजना मोड चालू या बंद करें",
        "sandbox और अनुमोदन नीति दिखाएँ",
        "वर्तमान सत्र निर्यात करें",
        "कार्य profile बदलें या बनाएँ",
        "मेमोरी और retrieval स्थिति दिखाएँ",
        "fallback मॉडल देखें या सेट करें",
        "अंतर्निहित और प्रोजेक्ट Skills दिखाएँ",
        "सत्र उपयोग और लागत दिखाएँ",
        "context window उपयोग दिखाएँ",
        "XDSH वातावरण जाँच चलाएँ",
        "प्रोजेक्ट एजेंट निर्देश बनाएँ",
        "कोड समीक्षा चलाएँ",
    ),
    "id": (
        "Mulai XDSH dan tampilkan cara penggunaan",
        "Tampilkan perintah dan bantuan tugas",
        "Tampilkan status tugas saat ini",
        "Pilih bahasa antarmuka dan jawaban",
        "Kelola antrean, prioritas, dan tindak lanjut",
        "Buat tugas latar belakang persisten",
        "Lihat dan kendalikan papan tugas persisten",
        "Kelola jadwal sekali, berkala, dan Cron",
        "Batalkan tugas aktif",
        "Buat dan pindah ke sesi baru",
        "Kelola dan pindah sesi",
        "Cari riwayat sesi saat ini atau semua sesi",
        "Hapus konteks DSH saat ini",
        "Pilih model sesi berdasarkan penyedia",
        "Atur tingkat penalaran LLM",
        "Tampilkan atau sembunyikan thinking terstruktur",
        "Kelola pembelajaran berbasis persetujuan",
        "Ambil teks web yang diizinkan",
        "Daftar atau panggil alat MCP yang disetujui",
        "Gunakan browser gateway terbatas",
        "Tampilkan batas media yang aman",
        "Temukan dan kelola plugin DSH",
        "Padatkan konteks percakapan",
        "Catat umpan balik sesi DSH ini",
        "Atur atau lihat tujuan tugas panjang",
        "Masuk atau keluar dari mode rencana",
        "Tampilkan kebijakan sandbox dan persetujuan",
        "Ekspor sesi saat ini",
        "Pindah atau buat profile kerja",
        "Tampilkan status memori dan pencarian",
        "Lihat atau atur model fallback",
        "Lihat Skills bawaan dan proyek",
        "Tampilkan penggunaan dan biaya sesi",
        "Tampilkan penggunaan jendela konteks",
        "Jalankan pemeriksaan lingkungan XDSH",
        "Buat instruksi agen proyek",
        "Jalankan tinjauan kode",
    ),
}


_MESSAGES: Final[dict[str, dict[str, str]]] = {
    "en": {
        "command.compatibility": "XDSH compatible",
        "language.title": "🌐 Language",
        "language.current": "Current: {language}",
        "language.choose": "Choose the interface and default answer language.",
        "language.saved": "✅ Language changed to {language}.",
        "language.invalid": "Unsupported language. Use /language to choose one.",
        "language.menu_warning": "The interface changed, but Telegram could not refresh the menu yet.",
        "welcome.title": "👋 XDSH is connected",
        "welcome.quick": "🚀 Quick start",
        "welcome.direct": "Send text directly to start a task.",
        "welcome.help": "Use /help for the complete command groups.",
        "welcome.commands": "📋 Command status",
        "welcome.executable": "{count}/{count} commands are executable.",
        "welcome.compat": "↔️ marks an RC2 Host gap handled by a transparent XDSH workflow.",
        "help.title": "📚 XDSH command guide",
        "help.tasks": "🚀 Tasks",
        "help.tasks_note": "Send text to run a DSH task; progress and answers are separate.",
        "help.sessions": "💬 Sessions",
        "help.sessions_note": "/session manages sessions; /search all <term> searches this chat.",
        "help.models": "🧠 Models and context",
        "help.models_note": "/model starts with Provider; /fallback runs once after a terminal failure.",
        "help.quality": "🎯 Goals, memory and quality",
        "help.quality_note": "Skill evolution requires an explicit diff review and approval.",
        "help.tools": "🔌 Tools and system",
        "help.tools_note": "Plugin changes require approval; DSH Web/CLI stays independent.",
        "help.compatibility": "💡 Compatibility",
        "help.compat_count": "{count}/{count} commands are executable.",
        "help.compat_routes": "RC2 Host routes provided by XDSH: {routes}",
        "help.compat_note": "DSH definitions win name conflicts; compatibility routes are never fake prompts.",
        "unknown.command": "Unknown command /{command}. Use /help to see the command catalog.",
        "message.unreadable": "⚠️ This message has no readable text or supported attachment.",
        "setup.ready": "XDSH root ready: {root} ({mode}); language={language}",
        "setup.counts": "created={created} existing={existing}",
        "setup.warning": "warning: {warning}",
    },
    "zh-TW": {
        "command.compatibility": "XDSH 相容",
        "language.title": "🌐 語言",
        "language.current": "目前：{language}",
        "language.choose": "選擇介面與預設回答語言。",
        "language.saved": "✅ 語言已切換為 {language}。",
        "language.invalid": "不支援這個語言。請使用 /language 選擇。",
        "language.menu_warning": "介面已切換，但 Telegram 選單暫時無法重新整理。",
        "welcome.title": "👋 XDSH 已連線",
        "welcome.quick": "🚀 快速開始",
        "welcome.direct": "直接傳送文字即可開始任務。",
        "welcome.help": "使用 /help 查看完整命令分組。",
        "welcome.commands": "📋 命令狀態",
        "welcome.executable": "{count}/{count} 個命令可執行。",
        "welcome.compat": "↔️ 表示 RC2 Host 接口缺口由透明的 XDSH 流程處理。",
        "help.title": "📚 XDSH 命令指南",
        "help.tasks": "🚀 任務",
        "help.tasks_note": "直接傳送文字會執行 DSH 任務；進度與答案分開傳送。",
        "help.sessions": "💬 會話",
        "help.sessions_note": "/session 管理多會話；/search all <詞> 搜尋本聊天全部會話。",
        "help.models": "🧠 模型與上下文",
        "help.models_note": "/model 先選 Provider；/fallback 只在一次終端故障時切換。",
        "help.quality": "🎯 目標、記憶與品質",
        "help.quality_note": "Skill 演化必須查看 diff 後明確批准。",
        "help.tools": "🔌 工具與系統",
        "help.tools_note": "插件變更須先批准；DSH Web/CLI 仍可獨立使用。",
        "help.compatibility": "💡 相容性",
        "help.compat_count": "{count}/{count} 個命令可執行。",
        "help.compat_routes": "由 XDSH 補足的 RC2 Host 路由：{routes}",
        "help.compat_note": "同名命令以 DSH 為準；相容流程不會偽裝成模型提示詞。",
        "unknown.command": "未知命令 /{command}。使用 /help 查看命令目錄。",
        "message.unreadable": "⚠️ 這則訊息沒有可讀文字或支援的附件。",
        "setup.ready": "XDSH 根目錄已就緒：{root}（{mode}）；語言={language}",
        "setup.counts": "已建立={created} 已存在={existing}",
        "setup.warning": "警告：{warning}",
    },
    "zh-CN": {
        "command.compatibility": "XDSH 兼容",
        "language.title": "🌐 语言",
        "language.current": "当前：{language}",
        "language.choose": "选择界面与默认回答语言。",
        "language.saved": "✅ 语言已切换为 {language}。",
        "language.invalid": "不支持这个语言。请使用 /language 选择。",
        "language.menu_warning": "界面已切换，但 Telegram 菜单暂时无法刷新。",
        "welcome.title": "👋 XDSH 已连接",
        "welcome.quick": "🚀 快速开始",
        "welcome.direct": "直接发送文字即可开始任务。",
        "welcome.help": "使用 /help 查看完整命令分组。",
        "welcome.commands": "📋 命令状态",
        "welcome.executable": "{count}/{count} 个命令可执行。",
        "welcome.compat": "↔️ 表示 RC2 Host 接口缺口由透明的 XDSH 流程处理。",
        "help.title": "📚 XDSH 命令指南",
        "help.tasks": "🚀 任务",
        "help.tasks_note": "直接发送文字会执行 DSH 任务；进度与答案分开发送。",
        "help.sessions": "💬 会话",
        "help.sessions_note": "/session 管理多会话；/search all <词> 搜索本聊天全部会话。",
        "help.models": "🧠 模型与上下文",
        "help.models_note": "/model 先选 Provider；/fallback 只在一次终端故障时切换。",
        "help.quality": "🎯 目标、记忆与质量",
        "help.quality_note": "Skill 演化必须查看 diff 后明确批准。",
        "help.tools": "🔌 工具与系统",
        "help.tools_note": "插件变更须先批准；DSH Web/CLI 仍可独立使用。",
        "help.compatibility": "💡 兼容性",
        "help.compat_count": "{count}/{count} 个命令可执行。",
        "help.compat_routes": "由 XDSH 补足的 RC2 Host 路由：{routes}",
        "help.compat_note": "同名命令以 DSH 为准；兼容流程不会伪装成模型提示词。",
        "unknown.command": "未知命令 /{command}。使用 /help 查看命令目录。",
        "message.unreadable": "⚠️ 这条消息没有可读文字或支持的附件。",
        "setup.ready": "XDSH 根目录已就绪：{root}（{mode}）；语言={language}",
        "setup.counts": "已创建={created} 已存在={existing}",
        "setup.warning": "警告：{warning}",
    },
    "ja": {
        "command.compatibility": "XDSH 互換",
        "language.title": "🌐 言語",
        "language.current": "現在：{language}",
        "language.choose": "画面と既定の回答言語を選択してください。",
        "language.saved": "✅ 言語を {language} に変更しました。",
        "language.invalid": "未対応の言語です。/language から選択してください。",
        "language.menu_warning": "画面言語は変更されましたが、Telegram メニューを更新できませんでした。",
        "welcome.title": "👋 XDSH に接続しました",
        "welcome.quick": "🚀 クイックスタート",
        "welcome.direct": "テキストを送るとタスクを開始します。",
        "welcome.help": "/help で全コマンドを表示します。",
        "welcome.commands": "📋 コマンド状態",
        "welcome.executable": "{count}/{count} コマンドを実行できます。",
        "welcome.compat": "↔️ は RC2 Host の不足を XDSH が透明に補うことを示します。",
        "help.title": "📚 XDSH コマンドガイド",
        "help.tasks": "🚀 タスク",
        "help.tasks_note": "テキストで DSH タスクを実行し、進捗と回答は別に送信されます。",
        "help.sessions": "💬 セッション",
        "help.sessions_note": "/session で管理し、/search all <語句> で全履歴を検索します。",
        "help.models": "🧠 モデルとコンテキスト",
        "help.models_note": "/model は Provider から選び、/fallback は終端障害時に一度だけ動きます。",
        "help.quality": "🎯 目標・メモリ・品質",
        "help.quality_note": "Skill の変更は diff 確認と明示承認が必要です。",
        "help.tools": "🔌 ツールとシステム",
        "help.tools_note": "プラグイン変更には承認が必要で、DSH Web/CLI は独立して使えます。",
        "help.compatibility": "💡 互換性",
        "help.compat_count": "{count}/{count} コマンドを実行できます。",
        "help.compat_routes": "XDSH が補完する RC2 Host ルート：{routes}",
        "help.compat_note": "同名は DSH を優先し、互換処理を偽のプロンプトにしません。",
        "unknown.command": "不明なコマンド /{command} です。/help を確認してください。",
        "message.unreadable": "⚠️ 読み取れるテキストまたは対応添付がありません。",
        "setup.ready": "XDSH ルート準備完了：{root}（{mode}）；言語={language}",
        "setup.counts": "作成={created} 既存={existing}",
        "setup.warning": "警告：{warning}",
    },
    "ko": {
        "command.compatibility": "XDSH 호환",
        "language.title": "🌐 언어",
        "language.current": "현재: {language}",
        "language.choose": "인터페이스와 기본 답변 언어를 선택하세요.",
        "language.saved": "✅ 언어를 {language}(으)로 변경했습니다.",
        "language.invalid": "지원하지 않는 언어입니다. /language에서 선택하세요.",
        "language.menu_warning": "언어는 변경됐지만 Telegram 메뉴를 아직 새로 고치지 못했습니다.",
        "welcome.title": "👋 XDSH가 연결되었습니다",
        "welcome.quick": "🚀 빠른 시작",
        "welcome.direct": "텍스트를 보내면 작업을 시작합니다.",
        "welcome.help": "/help에서 전체 명령을 확인하세요.",
        "welcome.commands": "📋 명령 상태",
        "welcome.executable": "{count}/{count}개 명령을 실행할 수 있습니다.",
        "welcome.compat": "↔️는 RC2 Host의 빈 부분을 XDSH가 투명하게 처리함을 뜻합니다.",
        "help.title": "📚 XDSH 명령 안내",
        "help.tasks": "🚀 작업",
        "help.tasks_note": "텍스트로 DSH 작업을 실행하며 진행과 답변은 분리됩니다.",
        "help.sessions": "💬 세션",
        "help.sessions_note": "/session으로 관리하고 /search all <검색어>로 전체 기록을 찾습니다.",
        "help.models": "🧠 모델과 컨텍스트",
        "help.models_note": "/model은 Provider부터 선택하며 /fallback은 최종 오류 뒤 한 번 실행됩니다.",
        "help.quality": "🎯 목표, 메모리, 품질",
        "help.quality_note": "Skill 변경은 diff 검토와 명시적 승인이 필요합니다.",
        "help.tools": "🔌 도구와 시스템",
        "help.tools_note": "플러그인 변경은 승인이 필요하며 DSH Web/CLI는 독립적으로 동작합니다.",
        "help.compatibility": "💡 호환성",
        "help.compat_count": "{count}/{count}개 명령을 실행할 수 있습니다.",
        "help.compat_routes": "XDSH가 보완하는 RC2 Host 경로: {routes}",
        "help.compat_note": "이름 충돌은 DSH를 우선하며 호환 경로를 가짜 프롬프트로 만들지 않습니다.",
        "unknown.command": "알 수 없는 명령 /{command}입니다. /help를 확인하세요.",
        "message.unreadable": "⚠️ 읽을 수 있는 텍스트나 지원 첨부가 없습니다.",
        "setup.ready": "XDSH 루트 준비 완료: {root} ({mode}); 언어={language}",
        "setup.counts": "생성={created} 기존={existing}",
        "setup.warning": "경고: {warning}",
    },
}


# First-use navigation is native for every supported language.  Less common
# product strings may still use the deterministic English fallback, but a
# selected locale never silently falls back to Chinese.
_MESSAGES.update(
    {
        "es": {
            "command.compatibility": "Compatible con XDSH",
            "language.title": "🌐 Idioma",
            "language.current": "Actual: {language}",
            "language.choose": "Elige el idioma de la interfaz y de las respuestas predeterminadas.",
            "language.saved": "✅ Idioma cambiado a {language}.",
            "language.invalid": "Idioma no compatible. Usa /language para elegir uno.",
            "language.menu_warning": "El idioma cambió, pero Telegram aún no pudo actualizar el menú.",
            "welcome.title": "👋 XDSH está conectado",
            "welcome.quick": "🚀 Inicio rápido",
            "welcome.direct": "Envía texto directamente para iniciar una tarea.",
            "welcome.help": "Usa /help para ver todos los grupos de comandos.",
            "welcome.commands": "📋 Estado de comandos",
            "welcome.executable": "Se pueden ejecutar {count}/{count} comandos.",
            "welcome.compat": "↔️ indica una carencia del Host RC2 resuelta de forma transparente por XDSH.",
            "help.title": "📚 Guía de comandos de XDSH",
            "help.tasks": "🚀 Tareas",
            "help.tasks_note": "Envía texto para ejecutar una tarea DSH; el progreso y la respuesta se envían por separado.",
            "help.sessions": "💬 Sesiones",
            "help.sessions_note": "/session administra sesiones; /search all <término> busca en este chat.",
            "help.models": "🧠 Modelos y contexto",
            "help.models_note": "/model comienza por Provider; /fallback actúa una vez tras un fallo terminal.",
            "help.quality": "🎯 Objetivos, memoria y calidad",
            "help.quality_note": "La evolución de Skills exige revisar el diff y aprobarlo explícitamente.",
            "help.tools": "🔌 Herramientas y sistema",
            "help.tools_note": "Los cambios de plugins requieren aprobación; DSH Web/CLI sigue siendo independiente.",
            "help.compatibility": "💡 Compatibilidad",
            "help.compat_count": "Se pueden ejecutar {count}/{count} comandos.",
            "help.compat_routes": "Rutas del Host RC2 proporcionadas por XDSH: {routes}",
            "help.compat_note": "DSH tiene prioridad en conflictos de nombre; las rutas compatibles nunca son prompts simulados.",
            "unknown.command": "Comando desconocido /{command}. Usa /help para ver el catálogo.",
            "message.unreadable": "⚠️ El mensaje no contiene texto legible ni un adjunto compatible.",
            "setup.ready": "Raíz de XDSH lista: {root} ({mode}); idioma={language}",
            "setup.counts": "creados={created} existentes={existing}",
            "setup.warning": "aviso: {warning}",
        },
        "fr": {
            "command.compatibility": "Compatible XDSH",
            "language.title": "🌐 Langue",
            "language.current": "Actuelle : {language}",
            "language.choose": "Choisissez la langue de l’interface et des réponses par défaut.",
            "language.saved": "✅ Langue changée en {language}.",
            "language.invalid": "Langue non prise en charge. Utilisez /language pour choisir.",
            "language.menu_warning": "La langue a changé, mais Telegram n’a pas encore pu actualiser le menu.",
            "welcome.title": "👋 XDSH est connecté",
            "welcome.quick": "🚀 Démarrage rapide",
            "welcome.direct": "Envoyez directement du texte pour lancer une tâche.",
            "welcome.help": "Utilisez /help pour afficher tous les groupes de commandes.",
            "welcome.commands": "📋 État des commandes",
            "welcome.executable": "{count}/{count} commandes sont exécutables.",
            "welcome.compat": "↔️ signale une lacune du Host RC2 gérée de façon transparente par XDSH.",
            "help.title": "📚 Guide des commandes XDSH",
            "help.tasks": "🚀 Tâches",
            "help.tasks_note": "Envoyez du texte pour exécuter une tâche DSH ; progression et réponse sont séparées.",
            "help.sessions": "💬 Sessions",
            "help.sessions_note": "/session gère les sessions ; /search all <terme> cherche dans ce chat.",
            "help.models": "🧠 Modèles et contexte",
            "help.models_note": "/model commence par Provider ; /fallback agit une fois après un échec terminal.",
            "help.quality": "🎯 Objectifs, mémoire et qualité",
            "help.quality_note": "L’évolution des Skills exige une revue du diff et une approbation explicite.",
            "help.tools": "🔌 Outils et système",
            "help.tools_note": "Les changements de plugins exigent une approbation ; DSH Web/CLI reste indépendant.",
            "help.compatibility": "💡 Compatibilité",
            "help.compat_count": "{count}/{count} commandes sont exécutables.",
            "help.compat_routes": "Routes du Host RC2 fournies par XDSH : {routes}",
            "help.compat_note": "DSH prévaut lors des conflits de noms ; les routes compatibles ne sont jamais des prompts simulés.",
            "unknown.command": "Commande inconnue /{command}. Utilisez /help pour voir le catalogue.",
            "message.unreadable": "⚠️ Ce message ne contient aucun texte lisible ni pièce jointe prise en charge.",
            "setup.ready": "Racine XDSH prête : {root} ({mode}) ; langue={language}",
            "setup.counts": "créés={created} existants={existing}",
            "setup.warning": "avertissement : {warning}",
        },
        "de": {
            "command.compatibility": "XDSH-kompatibel",
            "language.title": "🌐 Sprache",
            "language.current": "Aktuell: {language}",
            "language.choose": "Wähle die Sprache für Oberfläche und Standardantworten.",
            "language.saved": "✅ Sprache auf {language} geändert.",
            "language.invalid": "Nicht unterstützte Sprache. Mit /language auswählen.",
            "language.menu_warning": "Die Sprache wurde geändert, aber Telegram konnte das Menü noch nicht aktualisieren.",
            "welcome.title": "👋 XDSH ist verbunden",
            "welcome.quick": "🚀 Schnellstart",
            "welcome.direct": "Sende direkt Text, um eine Aufgabe zu starten.",
            "welcome.help": "Mit /help werden alle Befehlsgruppen angezeigt.",
            "welcome.commands": "📋 Befehlsstatus",
            "welcome.executable": "{count}/{count} Befehle sind ausführbar.",
            "welcome.compat": "↔️ kennzeichnet eine RC2-Host-Lücke, die XDSH transparent behandelt.",
            "help.title": "📚 XDSH-Befehlsübersicht",
            "help.tasks": "🚀 Aufgaben",
            "help.tasks_note": "Text startet eine DSH-Aufgabe; Fortschritt und Antwort werden getrennt gesendet.",
            "help.sessions": "💬 Sitzungen",
            "help.sessions_note": "/session verwaltet Sitzungen; /search all <Begriff> durchsucht diesen Chat.",
            "help.models": "🧠 Modelle und Kontext",
            "help.models_note": "/model beginnt mit Provider; /fallback läuft einmal nach einem endgültigen Fehler.",
            "help.quality": "🎯 Ziele, Gedächtnis und Qualität",
            "help.quality_note": "Skill-Änderungen erfordern Diff-Prüfung und ausdrückliche Freigabe.",
            "help.tools": "🔌 Werkzeuge und System",
            "help.tools_note": "Plugin-Änderungen erfordern Freigabe; DSH Web/CLI bleibt unabhängig.",
            "help.compatibility": "💡 Kompatibilität",
            "help.compat_count": "{count}/{count} Befehle sind ausführbar.",
            "help.compat_routes": "Von XDSH bereitgestellte RC2-Host-Routen: {routes}",
            "help.compat_note": "Bei Namenskonflikten hat DSH Vorrang; Kompatibilitätsrouten sind nie simulierte Prompts.",
            "unknown.command": "Unbekannter Befehl /{command}. Mit /help wird der Katalog angezeigt.",
            "message.unreadable": "⚠️ Diese Nachricht enthält keinen lesbaren Text oder unterstützten Anhang.",
            "setup.ready": "XDSH-Stammverzeichnis bereit: {root} ({mode}); Sprache={language}",
            "setup.counts": "erstellt={created} vorhanden={existing}",
            "setup.warning": "Warnung: {warning}",
        },
        "pt-BR": {
            "command.compatibility": "Compatível com XDSH",
            "language.title": "🌐 Idioma",
            "language.current": "Atual: {language}",
            "language.choose": "Escolha o idioma da interface e das respostas padrão.",
            "language.saved": "✅ Idioma alterado para {language}.",
            "language.invalid": "Idioma não suportado. Use /language para escolher.",
            "language.menu_warning": "O idioma mudou, mas o Telegram ainda não conseguiu atualizar o menu.",
            "welcome.title": "👋 XDSH está conectado",
            "welcome.quick": "🚀 Início rápido",
            "welcome.direct": "Envie texto diretamente para iniciar uma tarefa.",
            "welcome.help": "Use /help para ver todos os grupos de comandos.",
            "welcome.commands": "📋 Estado dos comandos",
            "welcome.executable": "{count}/{count} comandos estão disponíveis.",
            "welcome.compat": "↔️ indica uma lacuna do Host RC2 tratada de forma transparente pelo XDSH.",
            "help.title": "📚 Guia de comandos do XDSH",
            "help.tasks": "🚀 Tarefas",
            "help.tasks_note": "Envie texto para executar uma tarefa DSH; progresso e resposta são enviados separadamente.",
            "help.sessions": "💬 Sessões",
            "help.sessions_note": "/session gerencia sessões; /search all <termo> pesquisa neste chat.",
            "help.models": "🧠 Modelos e contexto",
            "help.models_note": "/model começa pelo Provider; /fallback atua uma vez após uma falha terminal.",
            "help.quality": "🎯 Metas, memória e qualidade",
            "help.quality_note": "A evolução de Skills exige revisão do diff e aprovação explícita.",
            "help.tools": "🔌 Ferramentas e sistema",
            "help.tools_note": "Mudanças de plugins exigem aprovação; DSH Web/CLI continua independente.",
            "help.compatibility": "💡 Compatibilidade",
            "help.compat_count": "{count}/{count} comandos estão disponíveis.",
            "help.compat_routes": "Rotas do Host RC2 fornecidas pelo XDSH: {routes}",
            "help.compat_note": "DSH prevalece em conflitos de nomes; rotas compatíveis nunca são prompts simulados.",
            "unknown.command": "Comando desconhecido /{command}. Use /help para ver o catálogo.",
            "message.unreadable": "⚠️ Esta mensagem não contém texto legível nem anexo compatível.",
            "setup.ready": "Raiz do XDSH pronta: {root} ({mode}); idioma={language}",
            "setup.counts": "criados={created} existentes={existing}",
            "setup.warning": "aviso: {warning}",
        },
        "ru": {
            "command.compatibility": "Совместимо с XDSH",
            "language.title": "🌐 Язык",
            "language.current": "Текущий: {language}",
            "language.choose": "Выберите язык интерфейса и ответов по умолчанию.",
            "language.saved": "✅ Язык изменён на {language}.",
            "language.invalid": "Язык не поддерживается. Выберите его через /language.",
            "language.menu_warning": "Язык изменён, но Telegram пока не смог обновить меню.",
            "welcome.title": "👋 XDSH подключён",
            "welcome.quick": "🚀 Быстрый старт",
            "welcome.direct": "Отправьте текст, чтобы запустить задачу.",
            "welcome.help": "Команда /help покажет все группы команд.",
            "welcome.commands": "📋 Состояние команд",
            "welcome.executable": "Доступно команд: {count}/{count}.",
            "welcome.compat": "↔️ отмечает пробел Host RC2, который XDSH прозрачно компенсирует.",
            "help.title": "📚 Справочник команд XDSH",
            "help.tasks": "🚀 Задачи",
            "help.tasks_note": "Отправьте текст для задачи DSH; прогресс и ответ приходят отдельно.",
            "help.sessions": "💬 Сессии",
            "help.sessions_note": "/session управляет сессиями; /search all <запрос> ищет в этом чате.",
            "help.models": "🧠 Модели и контекст",
            "help.models_note": "/model начинает с Provider; /fallback срабатывает один раз после конечной ошибки.",
            "help.quality": "🎯 Цели, память и качество",
            "help.quality_note": "Изменение Skills требует просмотра diff и явного одобрения.",
            "help.tools": "🔌 Инструменты и система",
            "help.tools_note": "Изменения plugins требуют одобрения; DSH Web/CLI остаётся независимым.",
            "help.compatibility": "💡 Совместимость",
            "help.compat_count": "Доступно команд: {count}/{count}.",
            "help.compat_routes": "Маршруты Host RC2, предоставленные XDSH: {routes}",
            "help.compat_note": "При совпадении имён приоритет у DSH; совместимые маршруты не имитируются prompts.",
            "unknown.command": "Неизвестная команда /{command}. Используйте /help для просмотра каталога.",
            "message.unreadable": "⚠️ В сообщении нет читаемого текста или поддерживаемого вложения.",
            "setup.ready": "Корень XDSH готов: {root} ({mode}); язык={language}",
            "setup.counts": "создано={created} уже было={existing}",
            "setup.warning": "предупреждение: {warning}",
        },
        "ar": {
            "command.compatibility": "متوافق مع XDSH",
            "language.title": "🌐 اللغة",
            "language.current": "الحالية: {language}",
            "language.choose": "اختر لغة الواجهة والردود الافتراضية.",
            "language.saved": "✅ تم تغيير اللغة إلى {language}.",
            "language.invalid": "اللغة غير مدعومة. استخدم /language للاختيار.",
            "language.menu_warning": "تغيّرت اللغة، لكن Telegram لم يتمكن من تحديث القائمة بعد.",
            "welcome.title": "👋 تم اتصال XDSH",
            "welcome.quick": "🚀 بدء سريع",
            "welcome.direct": "أرسل نصاً مباشرةً لبدء مهمة.",
            "welcome.help": "استخدم /help لعرض مجموعات الأوامر كاملةً.",
            "welcome.commands": "📋 حالة الأوامر",
            "welcome.executable": "يمكن تنفيذ {count}/{count} أمراً.",
            "welcome.compat": "↔️ يشير إلى نقص في Host RC2 يعالجه XDSH بشفافية.",
            "help.title": "📚 دليل أوامر XDSH",
            "help.tasks": "🚀 المهام",
            "help.tasks_note": "أرسل نصاً لتنفيذ مهمة DSH؛ يُرسل التقدم والجواب بشكل منفصل.",
            "help.sessions": "💬 الجلسات",
            "help.sessions_note": "يدير /session الجلسات؛ ويبحث /search all <عبارة> في هذه المحادثة.",
            "help.models": "🧠 النماذج والسياق",
            "help.models_note": "يبدأ /model باختيار Provider؛ ويعمل /fallback مرة بعد فشل نهائي.",
            "help.quality": "🎯 الأهداف والذاكرة والجودة",
            "help.quality_note": "يتطلب تطوير Skills مراجعة diff وموافقة صريحة.",
            "help.tools": "🔌 الأدوات والنظام",
            "help.tools_note": "تتطلب تغييرات plugins موافقة؛ ويبقى DSH Web/CLI مستقلاً.",
            "help.compatibility": "💡 التوافق",
            "help.compat_count": "يمكن تنفيذ {count}/{count} أمراً.",
            "help.compat_routes": "مسارات Host RC2 التي يوفرها XDSH: {routes}",
            "help.compat_note": "تكون الأولوية لـ DSH عند تعارض الأسماء؛ ولا تُحاكى مسارات التوافق كـ prompts.",
            "unknown.command": "الأمر /{command} غير معروف. استخدم /help لعرض القائمة.",
            "message.unreadable": "⚠️ لا تحتوي الرسالة على نص مقروء أو مرفق مدعوم.",
            "setup.ready": "مجلد XDSH جاهز: {root} ({mode})؛ اللغة={language}",
            "setup.counts": "تم الإنشاء={created} موجود مسبقاً={existing}",
            "setup.warning": "تحذير: {warning}",
        },
        "hi": {
            "command.compatibility": "XDSH संगत",
            "language.title": "🌐 भाषा",
            "language.current": "वर्तमान: {language}",
            "language.choose": "इंटरफ़ेस और डिफ़ॉल्ट उत्तर की भाषा चुनें।",
            "language.saved": "✅ भाषा {language} में बदल दी गई।",
            "language.invalid": "यह भाषा समर्थित नहीं है। /language से चुनें।",
            "language.menu_warning": "भाषा बदल गई, लेकिन Telegram मेनू अभी अपडेट नहीं हो सका।",
            "welcome.title": "👋 XDSH कनेक्ट हो गया है",
            "welcome.quick": "🚀 त्वरित शुरुआत",
            "welcome.direct": "कार्य शुरू करने के लिए सीधे टेक्स्ट भेजें।",
            "welcome.help": "सभी कमांड समूह देखने के लिए /help का उपयोग करें।",
            "welcome.commands": "📋 कमांड स्थिति",
            "welcome.executable": "{count}/{count} कमांड चलाए जा सकते हैं।",
            "welcome.compat": "↔️ RC2 Host की उस कमी को दर्शाता है जिसे XDSH पारदर्शी रूप से संभालता है।",
            "help.title": "📚 XDSH कमांड गाइड",
            "help.tasks": "🚀 कार्य",
            "help.tasks_note": "DSH कार्य चलाने के लिए टेक्स्ट भेजें; प्रगति और उत्तर अलग-अलग भेजे जाते हैं।",
            "help.sessions": "💬 सत्र",
            "help.sessions_note": "/session सत्रों को संभालता है; /search all <शब्द> इस चैट में खोजता है।",
            "help.models": "🧠 मॉडल और संदर्भ",
            "help.models_note": "/model पहले Provider चुनता है; /fallback अंतिम विफलता के बाद एक बार चलता है।",
            "help.quality": "🎯 लक्ष्य, मेमोरी और गुणवत्ता",
            "help.quality_note": "Skill बदलाव के लिए diff समीक्षा और स्पष्ट मंज़ूरी आवश्यक है।",
            "help.tools": "🔌 टूल और सिस्टम",
            "help.tools_note": "Plugin बदलाव के लिए मंज़ूरी चाहिए; DSH Web/CLI स्वतंत्र रहता है।",
            "help.compatibility": "💡 संगतता",
            "help.compat_count": "{count}/{count} कमांड चलाए जा सकते हैं।",
            "help.compat_routes": "XDSH द्वारा उपलब्ध RC2 Host रूट: {routes}",
            "help.compat_note": "नाम टकराने पर DSH को प्राथमिकता मिलती है; संगत रूट नकली prompts नहीं हैं।",
            "unknown.command": "अज्ञात कमांड /{command}। सूची के लिए /help का उपयोग करें।",
            "message.unreadable": "⚠️ इस संदेश में पढ़ने योग्य टेक्स्ट या समर्थित अटैचमेंट नहीं है।",
            "setup.ready": "XDSH रूट तैयार: {root} ({mode}); भाषा={language}",
            "setup.counts": "बनाए गए={created} पहले से मौजूद={existing}",
            "setup.warning": "चेतावनी: {warning}",
        },
        "id": {
            "command.compatibility": "Kompatibel dengan XDSH",
            "language.title": "🌐 Bahasa",
            "language.current": "Saat ini: {language}",
            "language.choose": "Pilih bahasa antarmuka dan jawaban bawaan.",
            "language.saved": "✅ Bahasa diubah ke {language}.",
            "language.invalid": "Bahasa tidak didukung. Gunakan /language untuk memilih.",
            "language.menu_warning": "Bahasa telah berubah, tetapi Telegram belum dapat memperbarui menu.",
            "welcome.title": "👋 XDSH telah terhubung",
            "welcome.quick": "🚀 Mulai cepat",
            "welcome.direct": "Kirim teks langsung untuk memulai tugas.",
            "welcome.help": "Gunakan /help untuk melihat semua kelompok perintah.",
            "welcome.commands": "📋 Status perintah",
            "welcome.executable": "{count}/{count} perintah dapat dijalankan.",
            "welcome.compat": "↔️ menandai kekurangan Host RC2 yang ditangani XDSH secara transparan.",
            "help.title": "📚 Panduan perintah XDSH",
            "help.tasks": "🚀 Tugas",
            "help.tasks_note": "Kirim teks untuk menjalankan tugas DSH; progres dan jawaban dikirim terpisah.",
            "help.sessions": "💬 Sesi",
            "help.sessions_note": "/session mengelola sesi; /search all <istilah> mencari dalam chat ini.",
            "help.models": "🧠 Model dan konteks",
            "help.models_note": "/model dimulai dari Provider; /fallback berjalan sekali setelah kegagalan akhir.",
            "help.quality": "🎯 Sasaran, memori, dan kualitas",
            "help.quality_note": "Evolusi Skill memerlukan tinjauan diff dan persetujuan eksplisit.",
            "help.tools": "🔌 Alat dan sistem",
            "help.tools_note": "Perubahan plugin memerlukan persetujuan; DSH Web/CLI tetap mandiri.",
            "help.compatibility": "💡 Kompatibilitas",
            "help.compat_count": "{count}/{count} perintah dapat dijalankan.",
            "help.compat_routes": "Rute Host RC2 yang disediakan XDSH: {routes}",
            "help.compat_note": "DSH diprioritaskan saat nama bertabrakan; rute kompatibilitas bukan prompts tiruan.",
            "unknown.command": "Perintah /{command} tidak dikenal. Gunakan /help untuk melihat katalog.",
            "message.unreadable": "⚠️ Pesan ini tidak memiliki teks yang terbaca atau lampiran yang didukung.",
            "setup.ready": "Root XDSH siap: {root} ({mode}); bahasa={language}",
            "setup.counts": "dibuat={created} sudah ada={existing}",
            "setup.warning": "peringatan: {warning}",
        },
    }
)

_LANGUAGE_INSTRUCTIONS: Final[dict[str, str]] = {
    "en": (
        "Respond in English unless the user explicitly requests another language. "
        "Preserve code, commands, identifiers, file names, and quoted text exactly."
    ),
    "zh-TW": (
        "除非使用者明確要求其他語言，否則使用繁體中文回答。"
        "程式碼、命令、識別符、檔名與引文必須保持原樣。"
    ),
    "zh-CN": (
        "除非用户明确要求其他语言，否则使用简体中文回答。"
        "代码、命令、标识符、文件名与引文必须保持原样。"
    ),
    "ja": "明示的な別言語指定がない限り日本語で回答し、コードや識別子は原文を保ってください。",
    "ko": "사용자가 다른 언어를 명시하지 않으면 한국어로 답하고 코드와 식별자는 그대로 유지하세요.",
    "es": "Responde en español salvo petición explícita; conserva código, comandos e identificadores.",
    "fr": "Répondez en français sauf demande explicite; conservez code, commandes et identifiants.",
    "de": "Antworte auf Deutsch, außer der Nutzer verlangt etwas anderes; Code und Bezeichner bleiben unverändert.",
    "pt-BR": "Responda em português salvo pedido explícito; preserve código, comandos e identificadores.",
    "ru": "Отвечай по-русски, если явно не указан другой язык; сохраняй код, команды и идентификаторы.",
    "ar": "أجب بالعربية ما لم يطلب المستخدم لغة أخرى صراحةً، وحافظ على الشيفرة والأوامر والمعرّفات.",
    "hi": "जब तक उपयोगकर्ता दूसरी भाषा न माँगे, हिन्दी में उत्तर दें; कोड, कमांड और पहचानकर्ता यथावत रखें।",
    "id": "Jawab dalam Bahasa Indonesia kecuali diminta lain; pertahankan kode, perintah, dan pengenal.",
}


def translate(locale: str, key: str, /, **values: object) -> str:
    """Translate one product key with deterministic English fallback."""

    if _SAFE_KEY.fullmatch(key) is None:
        raise ValueError("translation key is invalid")
    code = canonical_language(locale)
    template = _MESSAGES.get(code, {}).get(key)
    if template is None:
        template = _MESSAGES["en"].get(key)
    if template is None:
        raise KeyError(key)
    try:
        return template.format(**values)
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid values for translation key {key}") from error


def command_description(locale: str, name: str) -> str:
    """Return one fully localized Telegram command description."""

    code = canonical_language(locale)
    if name == "process":
        from xdsh.core.process_i18n import process_labels

        return process_labels(code)[0]
    if name == "limits":
        from xdsh.core.limits_i18n import limits_labels

        return limits_labels(code)[0]
    if name == "icon":
        return {
            "en": "Set semantic icon intensity",
            "zh-TW": "設定輸出語義圖標密度",
            "zh-CN": "设置输出语义图标密度",
        }.get(code, "Set semantic icon intensity")
    try:
        index = _COMMAND_NAMES.index(name)
    except ValueError as error:
        raise KeyError(name) from error
    return _COMMAND_ROWS[code][index]


_PROGRESS_HEADINGS: Final = {
    "en": ("Progress update", "Progress summary"),
    "zh-TW": ("進度更新", "進度摘要"),
    "zh-CN": ("进度更新", "进度摘要"),
    "ja": ("進捗更新", "進捗概要"),
    "ko": ("진행 상황", "진행 요약"),
    "es": ("Actualización del progreso", "Resumen del progreso"),
    "fr": ("Avancement", "Résumé de progression"),
    "de": ("Fortschritt", "Fortschrittsübersicht"),
    "pt-BR": ("Atualização do progresso", "Resumo do progresso"),
    "ru": ("Ход выполнения", "Итоги выполнения"),
    "ar": ("تحديث التقدم", "ملخص التقدم"),
    "hi": ("प्रगति अपडेट", "प्रगति सारांश"),
    "id": ("Pembaruan progres", "Ringkasan progres"),
}

_PROGRESS_LABEL_KEYS: Final = (
    "tools", "plan", "current", "next", "usage", "input", "output", "cache", "reasoning",
)
_PROGRESS_LABELS: Final = {
    "en": ("Tools", "Plan", "Current", "Next", "Usage", "in", "out", "cache", "reasoning"),
    "zh-TW": ("工具", "計畫", "目前", "下一步", "用量", "入", "出", "快取", "推理"),
    "zh-CN": ("工具", "计划", "当前", "下一步", "用量", "入", "出", "缓存", "推理"),
    "ja": ("ツール", "計画", "現在", "次", "使用量", "入力", "出力", "キャッシュ", "推論"),
    "ko": ("도구", "계획", "현재", "다음", "사용량", "입력", "출력", "캐시", "추론"),
    "es": ("Herramientas", "Plan", "Actual", "Siguiente", "Uso", "entrada", "salida", "caché", "razonamiento"),
    "fr": ("Outils", "Plan", "En cours", "Ensuite", "Utilisation", "entrée", "sortie", "cache", "raisonnement"),
    "de": ("Werkzeuge", "Plan", "Aktuell", "Weiter", "Verbrauch", "Eingabe", "Ausgabe", "Cache", "Schlussfolgern"),
    "pt-BR": ("Ferramentas", "Plano", "Atual", "Próximo", "Uso", "entrada", "saída", "cache", "raciocínio"),
    "ru": ("Инструменты", "План", "Сейчас", "Далее", "Использование", "вход", "выход", "кэш", "рассуждение"),
    "ar": ("الأدوات", "الخطة", "الحالي", "التالي", "الاستخدام", "إدخال", "إخراج", "ذاكرة التخزين", "الاستدلال"),
    "hi": ("उपकरण", "योजना", "वर्तमान", "अगला", "उपयोग", "इनपुट", "आउटपुट", "कैश", "तर्क"),
    "id": ("Alat", "Rencana", "Saat ini", "Berikutnya", "Penggunaan", "masukan", "keluaran", "cache", "penalaran"),
}


def progress_label(locale: str, key: str) -> str:
    """Localize fixed labels only; never perform substitutions on user data."""

    try:
        index = _PROGRESS_LABEL_KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _PROGRESS_LABELS[canonical_language(locale)][index]


def progress_heading(locale: str, *, final: bool) -> str:
    """Return a complete-catalog heading without an implicit English fallback."""

    return _PROGRESS_HEADINGS[canonical_language(locale)][int(final)]


_PLAN_SNAPSHOT: Final = {
    "en": "Plan snapshot {done}/{total} · last updated {age} ago",
    "zh-TW": "計畫快照 {done}/{total} · 上次更新於 {age} 前",
    "zh-CN": "计划快照 {done}/{total} · 上次更新于 {age} 前",
    "ja": "計画スナップショット {done}/{total} · 最終更新 {age} 前",
    "ko": "계획 스냅샷 {done}/{total} · 마지막 업데이트 {age} 전",
    "es": "Estado del plan {done}/{total} · última actualización hace {age}",
    "fr": "État du plan {done}/{total} · dernière mise à jour il y a {age}",
    "de": "Planstand {done}/{total} · zuletzt vor {age} aktualisiert",
    "pt-BR": "Estado do plano {done}/{total} · última atualização há {age}",
    "ru": "Состояние плана {done}/{total} · обновлено {age} назад",
    "ar": "حالة الخطة {done}/{total} · آخر تحديث منذ {age}",
    "hi": "योजना स्थिति {done}/{total} · पिछला अपडेट {age} पहले",
    "id": "Status rencana {done}/{total} · diperbarui {age} yang lalu",
}


def plan_snapshot_text(locale: str, *, done: int, total: int, age: str) -> str:
    """Label the last Todo report as a timestamped snapshot, not live progress."""

    return _PLAN_SNAPSHOT[canonical_language(locale)].format(done=done, total=total, age=age)


# Fixed product state names only: never translate arbitrary tool/model payloads.
_PHASE_KEYS: Final = (
    "分析中", "準備工具輸入", "推理中", "模型重試", "模型限流", "整理輸出",
    "執行工具", "上下文壓縮", "壓縮後整理", "壓縮重試", "子代理執行",
    "執行環境就緒", "工具失敗", "分析結果", "更新計畫", "發生錯誤",
    "接近保護上限", "保護性停止", "已取消", "已結束", "完成",
)
_PHASE_ROWS: Final[dict[str, tuple[str, ...]]] = {
    "zh-TW": _PHASE_KEYS,
    "zh-CN": ("分析中", "准备工具输入", "推理中", "模型重试", "模型限流", "整理输出",
              "执行工具", "上下文压缩", "压缩后整理", "压缩重试", "子代理执行",
              "执行环境就绪", "工具失败", "分析结果", "更新计划", "发生错误",
              "接近保护上限", "保护性停止", "已取消", "已结束", "完成"),
    "en": ("Analyzing", "Preparing tool input", "Reasoning", "Model retry", "Model rate limit",
           "Preparing output", "Running tools", "Compacting context", "Finalizing compaction",
           "Compaction retry", "Running subagents", "Runtime ready", "Tool failure",
           "Analyzing results", "Updating plan", "Error", "Near protection limit",
           "Protection stop", "Cancelled", "Finished", "Complete"),
    "ja": ("分析中", "ツール入力準備中", "推論中", "モデル再試行", "モデル利用制限",
           "出力整理中", "ツール実行中", "コンテキスト圧縮中", "圧縮後の整理", "圧縮再試行",
           "サブエージェント実行中", "実行環境準備完了", "ツール処理失敗", "結果分析中",
           "計画更新中", "エラー", "保護上限に接近", "保護のため停止", "キャンセル済み", "終了", "完了"),
    "ko": ("분석 중", "도구 입력 준비 중", "추론 중", "모델 재시도", "모델 요청 제한",
           "출력 준비 중", "도구 실행 중", "컨텍스트 압축 중", "압축 마무리", "압축 재시도",
           "하위 에이전트 실행 중", "실행 환경 준비 완료", "도구 처리 실패", "결과 분석 중",
           "계획 갱신 중", "오류", "보호 한도에 근접", "보호를 위해 중지", "취소됨", "종료됨", "완료"),
    "es": ("Analizando", "Preparando entrada", "Razonando", "Reintento del modelo",
           "Límite de solicitudes", "Preparando respuesta", "Ejecutando herramientas",
           "Comprimiendo contexto", "Finalizando compresión", "Reintentando compresión",
           "Ejecutando subagentes", "Entorno listo", "Fallo de herramienta", "Analizando resultados",
           "Actualizando plan", "Error", "Cerca del límite de protección", "Parada de protección",
           "Cancelado", "Finalizado", "Completado"),
    "fr": ("Analyse", "Préparation des entrées", "Raisonnement", "Nouvel essai du modèle",
           "Limite de requêtes", "Préparation de la réponse", "Exécution des outils",
           "Compression du contexte", "Finalisation de la compression", "Nouvel essai de compression",
           "Exécution des sous-agents", "Environnement prêt", "Échec d’un outil", "Analyse des résultats",
           "Mise à jour du plan", "Erreur", "Limite de protection proche", "Arrêt de protection",
           "Annulé", "Terminé", "Achevé"),
    "de": ("Analyse", "Werkzeugeingabe vorbereiten", "Schlussfolgern", "Modell erneut versuchen",
           "Anfragelimit", "Ausgabe vorbereiten", "Werkzeuge ausführen", "Kontext komprimieren",
           "Komprimierung abschließen", "Komprimierung wiederholen", "Unteragenten ausführen",
           "Laufzeit bereit", "Werkzeug fehlgeschlagen", "Ergebnisse analysieren", "Plan aktualisieren",
           "Fehler", "Schutzlimit fast erreicht", "Schutzstopp", "Abgebrochen", "Beendet", "Abgeschlossen"),
    "pt-BR": ("Analisando", "Preparando entrada", "Raciocinando", "Nova tentativa do modelo",
              "Limite de solicitações", "Preparando resposta", "Executando ferramentas",
              "Compactando contexto", "Finalizando compactação", "Repetindo compactação",
              "Executando subagentes", "Ambiente pronto", "Falha de ferramenta", "Analisando resultados",
              "Atualizando plano", "Erro", "Perto do limite de proteção", "Parada de proteção",
              "Cancelado", "Encerrado", "Concluído"),
    "ru": ("Анализ", "Подготовка входных данных", "Рассуждение", "Повтор запроса к модели",
           "Ограничение запросов", "Подготовка ответа", "Выполнение инструментов", "Сжатие контекста",
           "Завершение сжатия", "Повтор сжатия", "Выполнение подагентов", "Среда готова",
           "Сбой инструмента", "Анализ результатов", "Обновление плана", "Ошибка",
           "Приближение к защитному лимиту", "Защитная остановка", "Отменено", "Завершено", "Выполнено"),
    "ar": ("جارٍ التحليل", "إعداد مدخلات الأداة", "جارٍ الاستدلال", "إعادة محاولة النموذج",
           "حد الطلبات", "إعداد الإجابة", "تشغيل الأدوات", "ضغط السياق", "إنهاء الضغط",
           "إعادة محاولة الضغط", "تشغيل الوكلاء الفرعيين", "بيئة التشغيل جاهزة", "تعذر تشغيل الأداة",
           "تحليل النتائج", "تحديث الخطة", "خطأ", "اقتراب حد الحماية", "إيقاف للحماية",
           "تم الإلغاء", "انتهى التنفيذ", "اكتمل"),
    "hi": ("विश्लेषण जारी", "उपकरण इनपुट तैयार हो रहा है", "तर्क जारी", "मॉडल पुनः प्रयास",
           "अनुरोध सीमा", "उत्तर तैयार हो रहा है", "उपकरण चल रहे हैं", "संदर्भ संपीड़न जारी",
           "संपीड़न पूरा हो रहा है", "संपीड़न पुनः प्रयास", "उप-एजेंट चल रहे हैं", "रनटाइम तैयार",
           "उपकरण विफल", "परिणाम विश्लेषण", "योजना अपडेट", "त्रुटि", "सुरक्षा सीमा निकट",
           "सुरक्षा के लिए रोका गया", "रद्द", "समाप्त", "पूर्ण"),
    "id": ("Menganalisis", "Menyiapkan masukan alat", "Menalar", "Mencoba ulang model",
           "Batas permintaan", "Menyiapkan jawaban", "Menjalankan alat", "Memadatkan konteks",
           "Menyelesaikan pemadatan", "Mengulangi pemadatan", "Menjalankan subagen", "Lingkungan siap",
           "Alat gagal", "Menganalisis hasil", "Memperbarui rencana", "Kesalahan",
           "Mendekati batas perlindungan", "Dihentikan untuk perlindungan", "Dibatalkan", "Berakhir", "Selesai"),
}


def progress_phase(locale: str, phase: str) -> str:
    """Translate known host phases, preserving unknown extension identifiers."""

    code = canonical_language(locale)
    try:
        index = _PHASE_KEYS.index(phase)
    except ValueError:
        return phase
    return _PHASE_ROWS[code][index]


_TOOL_TERM_KEYS: Final = (
    "Terminal", "Read", "Edit", "Write", "File search", "Content search", "Search", "Todo",
    "Web search", "Fetch web page", "Browser", "Subagent", "Image", "Memory", "Skill",
    "Create", "Update", "Delete",
)
_TOOL_TERM_ROWS: Final[dict[str, tuple[str, ...]]] = {
    "en": _TOOL_TERM_KEYS,
    "zh-TW": ("終端", "讀取", "編輯", "寫入", "檔案搜尋", "內容搜尋", "搜尋", "待辦",
              "網路搜尋", "讀取網頁", "瀏覽器", "子代理", "圖像", "記憶", "技能", "建立", "更新", "刪除"),
    "zh-CN": ("终端", "读取", "编辑", "写入", "文件搜索", "内容搜索", "搜索", "待办",
              "网络搜索", "读取网页", "浏览器", "子代理", "图像", "记忆", "技能", "创建", "更新", "删除"),
    "ja": ("端末", "読み取り", "編集", "書き込み", "ファイル検索", "内容検索", "検索", "タスク一覧",
           "ウェブ検索", "ページ取得", "ブラウザー", "サブエージェント", "画像", "記憶", "スキル", "作成", "更新", "削除"),
    "ko": ("터미널", "읽기", "편집", "쓰기", "파일 검색", "내용 검색", "검색", "할 일",
           "웹 검색", "웹 페이지 가져오기", "브라우저", "하위 에이전트", "이미지", "기억", "스킬", "생성", "갱신", "삭제"),
    "es": ("Terminal", "Lectura", "Edición", "Escritura", "Búsqueda de archivos", "Búsqueda de contenido",
           "Búsqueda", "Tareas", "Búsqueda web", "Obtener página", "Navegador", "Subagente", "Imagen",
           "Memoria", "Habilidad", "Crear", "Actualizar", "Eliminar"),
    "fr": ("Terminal", "Lecture", "Modification", "Écriture", "Recherche de fichiers", "Recherche de contenu",
           "Recherche", "Tâches", "Recherche web", "Lecture de page", "Navigateur", "Sous-agent", "Image",
           "Mémoire", "Compétence", "Créer", "Mettre à jour", "Supprimer"),
    "de": ("Terminal", "Lesen", "Bearbeiten", "Schreiben", "Dateisuche", "Inhaltssuche", "Suche", "Aufgaben",
           "Websuche", "Webseite abrufen", "Browser", "Unteragent", "Bild", "Gedächtnis", "Fähigkeit",
           "Erstellen", "Aktualisieren", "Löschen"),
    "pt-BR": ("Terminal", "Leitura", "Edição", "Escrita", "Busca de arquivos", "Busca de conteúdo", "Busca",
              "Tarefas", "Busca na web", "Obter página", "Navegador", "Subagente", "Imagem", "Memória",
              "Habilidade", "Criar", "Atualizar", "Excluir"),
    "ru": ("Терминал", "Чтение", "Редактирование", "Запись", "Поиск файлов", "Поиск по содержимому",
           "Поиск", "Задачи", "Поиск в интернете", "Загрузка страницы", "Браузер", "Подагент", "Изображение",
           "Память", "Навык", "Создание", "Обновление", "Удаление"),
    "ar": ("الطرفية", "قراءة", "تحرير", "كتابة", "بحث الملفات", "بحث المحتوى", "بحث", "المهام",
           "بحث الويب", "جلب صفحة", "المتصفح", "وكيل فرعي", "صورة", "الذاكرة", "مهارة", "إنشاء", "تحديث", "حذف"),
    "hi": ("टर्मिनल", "पढ़ना", "संपादन", "लिखना", "फ़ाइल खोज", "सामग्री खोज", "खोज", "कार्य सूची",
           "वेब खोज", "पृष्ठ प्राप्त करना", "ब्राउज़र", "उप-एजेंट", "चित्र", "स्मृति", "कौशल", "बनाना", "अपडेट", "हटाना"),
    "id": ("Terminal", "Baca", "Edit", "Tulis", "Pencarian berkas", "Pencarian isi", "Pencarian", "Tugas",
           "Pencarian web", "Ambil halaman", "Peramban", "Subagen", "Gambar", "Memori", "Keterampilan",
           "Buat", "Perbarui", "Hapus"),
}


def tool_term(locale: str, term: str) -> str:
    """Localize a known tool category, not executable names or arguments."""

    code = canonical_language(locale)
    try:
        index = _TOOL_TERM_KEYS.index(term)
    except ValueError:
        return term
    return _TOOL_TERM_ROWS[code][index]


_TERMINAL_KEYS: Final = (
    "cancelled", "connection_lost", "missing_error", "empty", "retained", "no_partial", "error",
)
_TERMINAL_ROWS: Final[dict[str, tuple[str, ...]]] = {
    "en": ("Task cancelled.", "DSH connection ended without a finish event.",
           "DSH ended without an error code.", "DSH returned no displayable content.",
           "Partial output was retained.", "There is no partial output to retain.",
           "Execution error ({code}). {retained}"),
    "zh-TW": ("任務已取消。", "DSH 連線中斷: 未收到結束事件。", "DSH 結束時未提供錯誤代碼。",
              "DSH 未返回可顯示的內容。", "已保留部分輸出。", "目前沒有可保留的輸出。",
              "執行錯誤 ({code})。{retained}"),
    "zh-CN": ("任务已取消。", "DSH 连接中断: 未收到结束事件。", "DSH 结束时未提供错误代码。",
              "DSH 未返回可显示的内容。", "已保留部分输出。", "目前没有可保留的输出。",
              "执行错误 ({code})。{retained}"),
    "ja": ("タスクをキャンセルしました。", "終了イベントを受信せずに DSH 接続が終了しました。",
           "DSH がエラーコードを返さずに終了しました。", "DSH から表示できる内容が返されませんでした。",
           "部分的な出力を保持しました。", "保持できる出力はありません。", "実行エラー ({code})。{retained}"),
    "ko": ("작업이 취소되었습니다.", "종료 이벤트 없이 DSH 연결이 끝났습니다.",
           "DSH가 오류 코드 없이 종료되었습니다.", "DSH가 표시할 내용을 반환하지 않았습니다.",
           "일부 출력을 보존했습니다.", "보존할 출력이 없습니다.", "실행 오류 ({code}). {retained}"),
    "es": ("Tarea cancelada.", "La conexión DSH terminó sin evento de finalización.",
           "DSH terminó sin código de error.", "DSH no devolvió contenido visible.",
           "Se conservó la salida parcial.", "No hay salida parcial que conservar.", "Error de ejecución ({code}). {retained}"),
    "fr": ("Tâche annulée.", "La connexion DSH s’est terminée sans événement de fin.",
           "DSH s’est terminé sans code d’erreur.", "DSH n’a renvoyé aucun contenu affichable.",
           "La sortie partielle a été conservée.", "Aucune sortie partielle à conserver.", "Erreur d’exécution ({code}). {retained}"),
    "de": ("Aufgabe abgebrochen.", "Die DSH-Verbindung endete ohne Abschlussereignis.",
           "DSH endete ohne Fehlercode.", "DSH hat keinen darstellbaren Inhalt zurückgegeben.",
           "Teilausgabe wurde erhalten.", "Keine Teilausgabe zum Aufbewahren vorhanden.", "Ausführungsfehler ({code}). {retained}"),
    "pt-BR": ("Tarefa cancelada.", "A conexão DSH terminou sem evento de conclusão.",
              "DSH terminou sem código de erro.", "DSH não retornou conteúdo exibível.",
              "A saída parcial foi preservada.", "Não há saída parcial para preservar.", "Erro de execução ({code}). {retained}"),
    "ru": ("Задача отменена.", "Соединение DSH завершилось без события окончания.",
           "DSH завершился без кода ошибки.", "DSH не вернул содержимого для отображения.",
           "Частичный результат сохранён.", "Нет частичного результата для сохранения.", "Ошибка выполнения ({code}). {retained}"),
    "ar": ("تم إلغاء المهمة.", "انتهى اتصال DSH دون حدث انتهاء.", "انتهى DSH دون رمز خطأ.",
           "لم يُرجع DSH محتوى قابلاً للعرض.", "تم الاحتفاظ بالمخرجات الجزئية.",
           "لا توجد مخرجات جزئية للاحتفاظ بها.", "خطأ في التنفيذ ({code}). {retained}"),
    "hi": ("कार्य रद्द कर दिया गया।", "समाप्ति घटना के बिना DSH कनेक्शन समाप्त हुआ।",
           "DSH बिना त्रुटि कोड के समाप्त हुआ।", "DSH ने दिखाने योग्य सामग्री नहीं लौटाई।",
           "आंशिक आउटपुट सुरक्षित रखा गया।", "सुरक्षित रखने के लिए कोई आंशिक आउटपुट नहीं है।",
           "निष्पादन त्रुटि ({code})। {retained}"),
    "id": ("Tugas dibatalkan.", "Koneksi DSH berakhir tanpa peristiwa selesai.",
           "DSH berakhir tanpa kode kesalahan.", "DSH tidak mengembalikan konten yang dapat ditampilkan.",
           "Keluaran sebagian dipertahankan.", "Tidak ada keluaran sebagian untuk disimpan.", "Kesalahan eksekusi ({code}). {retained}"),
}


def terminal_text(locale: str, key: str, **values: object) -> str:
    """Render a product terminal message with preserved technical fields."""

    try:
        index = _TERMINAL_KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _TERMINAL_ROWS[canonical_language(locale)][index].format(**values)


def language_instruction(locale: str) -> str:
    selected = canonical_language(locale)
    spec = language_spec(selected)
    return (
        f"{_LANGUAGE_INSTRUCTIONS[selected]}\n"
        f"Selected response language: {selected} ({spec.native_name}). "
        "Use this language for all user-facing headings, explanations, checklists, "
        "progress descriptions, short status text emitted before tool calls, task-list "
        "item wording, conclusions and follow-up questions. Every public process excerpt "
        "must use this selected language too. A source language "
        "is not a language-switch request: tool results, retrieved pages, old conversation "
        "messages and examples must not change this selection. Only an explicit user "
        "request for a different output language overrides it for the requested content. "
        "Preserve code, commands, paths, model names, API identifiers, required JSON keys, "
        "exact-output literals and necessary direct quotations. Established technical "
        "terms may stay in English; explain them in the selected language when needed. "
        "Do not translate or append prose to machine-readable output contracts."
    )


def validate_catalogs() -> None:
    """Fail loudly when a release has an incomplete or unsafe locale pack."""

    expected = set(language_codes())
    if set(_PROGRESS_HEADINGS) != expected:
        raise ValueError("progress heading coverage mismatch")
    if set(_PHASE_ROWS) != expected or any(
        len(row) != len(_PHASE_KEYS) or any(not value for value in row)
        for row in _PHASE_ROWS.values()
    ):
        raise ValueError("progress phase coverage mismatch")
    if set(_TOOL_TERM_ROWS) != expected or any(
        len(row) != len(_TOOL_TERM_KEYS) or any(not value for value in row)
        for row in _TOOL_TERM_ROWS.values()
    ):
        raise ValueError("tool term coverage mismatch")
    if set(_TERMINAL_ROWS) != expected or any(
        len(row) != len(_TERMINAL_KEYS) or any(not value for value in row)
        for row in _TERMINAL_ROWS.values()
    ):
        raise ValueError("terminal message coverage mismatch")
    for row in _TERMINAL_ROWS.values():
        for value, english in zip(row, _TERMINAL_ROWS["en"], strict=True):
            if _fields(value) != _fields(english):
                raise ValueError("terminal message placeholder mismatch")
    if set(_PROGRESS_LABELS) != expected or any(
        len(row) != len(_PROGRESS_LABEL_KEYS) or any(not label for label in row)
        for row in _PROGRESS_LABELS.values()
    ):
        raise ValueError("progress label coverage mismatch")
    if set(_COMMAND_ROWS) != expected or set(_LANGUAGE_INSTRUCTIONS) != expected:
        raise ValueError("language catalog coverage mismatch")
    if set(_MESSAGES) != expected:
        raise ValueError("navigation catalog coverage mismatch")
    for code, row in _COMMAND_ROWS.items():
        if len(row) != len(_COMMAND_NAMES):
            raise ValueError(f"command catalog length mismatch for {code}")
        if any(not value.strip() or len(value) > 256 for value in row):
            raise ValueError(f"invalid command description for {code}")
    english_keys = set(_MESSAGES["en"])
    for code, messages in _MESSAGES.items():
        if set(messages) != english_keys:
            raise ValueError(f"navigation translation coverage mismatch for {code}")
        for key, template in messages.items():
            if key not in english_keys:
                raise ValueError(f"unknown translation key {key} in {code}")
            if _fields(template) != _fields(_MESSAGES["en"][key]):
                raise ValueError(f"placeholder mismatch for {code}:{key}")


def _fields(template: str) -> frozenset[str]:
    return frozenset(
        name
        for _, name, _, _ in string.Formatter().parse(template)
        if name is not None
    )


validate_catalogs()

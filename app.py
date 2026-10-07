"""Main application entry point for Zellinho Chat."""

import os
import sys
import uvicorn
from config import Settings


def print_separator(char="=", length=80):
    """Imprime separador."""
    print(char * length)


def print_section(title: str):
    """Imprime cabeçalho de seção."""
    print()
    print_separator()
    print(f" {title}")
    print_separator()


def validate_configuration(settings: Settings) -> bool:
    """
    Valida configuração obrigatória.

    Args:
        settings: Configurações da aplicação

    Returns:
        True se configuração válida, False caso contrário
    """
    errors = []
    warnings = []

    # Validar OpenAI API Key
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY == "your-openai-api-key-here":
        errors.append("OPENAI_API_KEY não configurada")

    # Validar RAG (se habilitado)
    if settings.RAG_ENABLED:
        if not settings.PINECONE_API_KEY:
            warnings.append("RAG habilitado mas PINECONE_API_KEY não configurada")

    # Validar MinIO (se configurado)
    if settings.MINIO_ENABLED and settings.MINIO_ENDPOINT:
        if not settings.MINIO_ACCESS_KEY or not settings.MINIO_SECRET_KEY:
            warnings.append("MinIO endpoint configurado mas credenciais faltando")

    # Validar Zellu Backend Integration
    if settings.ZELLU_BACKEND_WEBHOOK_URL:
        print("[CONFIG] ✓ Zellu Backend Webhook configurado")
    else:
        warnings.append("ZELLU_BACKEND_WEBHOOK_URL não configurado - modo simulação ativo")

    # Imprimir erros
    if errors:
        print_section("⚠️  ERROS DE CONFIGURAÇÃO")
        for error in errors:
            print(f"  ✗ {error}")
        return False

    # Imprimir avisos
    if warnings:
        print_section("⚠️  AVISOS DE CONFIGURAÇÃO")
        for warning in warnings:
            print(f"  ! {warning}")

    return True


def print_webhook_info(settings: Settings):
    """
    Imprime informações sobre webhooks configurados.

    Args:
        settings: Configurações da aplicação
    """
    print_section("🔗 WEBHOOKS CONFIGURADOS")

    # Endpoint de recebimento (nosso servidor)
    service_url = settings.SERVICE_BASE_URL
    print(f"\n📥 Endpoint de RECEBIMENTO (Zellu → Nossa IA):")
    print(f"   {service_url}/api/webhooks/company/ticket-response")

    print(f"\n📥 Endpoint MOCK (Simulação de Empresa):")
    print(f"   {service_url}/api/webhooks/company/ticket-response/mock")

    # Endpoint de envio (backend Zellu)
    if settings.ZELLU_BACKEND_WEBHOOK_URL:
        print(f"\n📤 Endpoint de ENVIO (Nossa IA → Zellu):")
        print(f"   {settings.ZELLU_BACKEND_WEBHOOK_URL}")
    else:
        print(f"\n📤 Endpoint de ENVIO: NÃO CONFIGURADO")
        print(f"   Modo simulação: mensagens serão marcadas como enviadas mas não serão de fato enviadas")

    # Configuração do Supabase
    print(f"\n📝 Configuração no Supabase SystemSettings:")
    print(f"   COMPANY_RESPONSE_WEBHOOK_URL_MOCK = {service_url}/api/webhooks/company/ticket-response/mock")

    # Instruções de ngrok
    if "localhost" in service_url or "127.0.0.1" in service_url:
        print_section("🌐 CONFIGURAÇÃO NGROK (DESENVOLVIMENTO)")
        print(f"\n⚠️  SERVICE_BASE_URL está configurado para localhost.")
        print(f"\nPara testar webhooks com o backend Zellu:")
        print(f"\n1. Inicie ngrok:")
        print(f"   ngrok http {settings.API_PORT}")
        print(f"\n2. Copie a URL gerada (ex: https://abc123.ngrok.io)")
        print(f"\n3. Atualize no .env:")
        print(f"   SERVICE_BASE_URL=https://abc123.ngrok.io")
        print(f"\n4. Atualize no Supabase SystemSettings:")
        print(f"   COMPANY_RESPONSE_WEBHOOK_URL_MOCK=https://abc123.ngrok.io/api/webhooks/company/ticket-response/mock")
        print(f"\n5. Reinicie o servidor: python app.py")


def print_startup_summary(settings: Settings, is_production: bool):
    """
    Imprime resumo de inicialização.

    Args:
        settings: Configurações da aplicação
        is_production: Se está em produção
    """
    print_section("🚀 SERVIDOR INICIANDO")

    print(f"\nAmbiente:     {'PRODUÇÃO' if is_production else 'DESENVOLVIMENTO'}")
    print(f"Host:         {settings.API_HOST}")
    print(f"Porta:        {settings.API_PORT}")
    print(f"URL Base:     http://{settings.API_HOST}:{settings.API_PORT}")
    print(f"Service URL:  {settings.SERVICE_BASE_URL}")
    print(f"Modelo LLM:   {settings.OPENAI_MODEL}")

    print(f"\nRuntime ativo:")
    print(f"  ✓ Open Dots - Intake e orquestração do chat")
    print(f"  ✓ Serviços de empresa, documentos, RAG e negociação")

    print(f"\nEndpoints principais:")
    print(f"  POST /message - Enviar mensagem")
    print(f"  GET  /state/{{chat_id}} - Obter estado")
    print(f"  GET  /health - Health check")
    print(f"  POST /api/webhooks/company/ticket-response - Receber resposta da empresa")
    print(f"  POST /api/webhooks/company/ticket-response/mock - Endpoint mock")
    print(f"  POST /api/ai/validate-company - Validar empresa na base Zellu")

    print(f"\nDocumentação:")
    print(f"  Swagger UI: http://{settings.API_HOST}:{settings.API_PORT}/docs")
    print(f"  ReDoc:      http://{settings.API_HOST}:{settings.API_PORT}/redoc")

    print_separator()
    print()


if __name__ == "__main__":
    # Limpar terminal (opcional)
    os.system('cls' if os.name == 'nt' else 'clear')

    print_separator("=", 80)
    print(" ZELLINHO - SISTEMA DE NEGOCIAÇÃO INTELIGENTE ".center(80))
    print_separator("=", 80)

    # Carregar configurações
    settings = Settings()

    # Detectar ambiente
    # Coolify injeta variaveis COOLIFY_* no container; Railway/Render mantidos por compatibilidade.
    is_production = bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RENDER")
        or os.getenv("COOLIFY_RESOURCE_UUID")
        or os.getenv("COOLIFY_FQDN")
        or os.getenv("ENVIRONMENT", "").lower() == "production"
    )

    # Validar configuração
    print_section("⚙️  VALIDAÇÃO DE CONFIGURAÇÃO")
    if not validate_configuration(settings):
        print("\n❌ Configuração inválida. Corrija os erros acima e tente novamente.\n")
        sys.exit(1)

    print("\n✅ Configuração validada com sucesso!")

    # Imprimir informações de webhooks
    print_webhook_info(settings)

    # Imprimir resumo de inicialização
    print_startup_summary(settings, is_production)

    # Configurar reload
    is_windows = sys.platform.startswith('win')
    # Auto-reload so quando pedido explicitamente (UVICORN_RELOAD=true) em dev.
    # Antes ficava ligado sempre que RAILWAY_ENVIRONMENT/RENDER nao existiam, o
    # que incluia o Coolify: o processo reiniciava a cada arquivo escrito em
    # disco (uploads, documentos gerados) e atrapalhava o healthcheck.
    enable_reload = (
        os.getenv("UVICORN_RELOAD", "false").lower() in ("1", "true", "yes")
        and not is_production
        and not is_windows
    )

    if is_windows and not is_production:
        print("ℹ️  Windows detectado: auto-reload desabilitado")
        print()

    # Iniciar servidor
    try:
        uvicorn.run(
            "src.main:app",
            host=settings.API_HOST,
            port=settings.API_PORT,
            reload=enable_reload,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n\n👋 Servidor encerrado pelo usuário\n")
    except Exception as e:
        print(f"\n❌ Erro ao iniciar servidor: {e}\n")
        sys.exit(1)

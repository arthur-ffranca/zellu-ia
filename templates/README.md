# Templates de Documentos - Zellinho

Este diretório contém os templates HTML usados pelo **WriterAgent** para gerar documentos automaticamente após a conclusão da conversa.

## 📁 Estrutura

```
templates/
├── extrajudicial.html      # Notificação extrajudicial
├── judicial.html           # Petição inicial judicial
├── email_amigavel.html     # E-mail de solução amigável
└── README.md              # Este arquivo
```

## 🎯 Como Funciona

O **WriterAgent** é acionado automaticamente após:
1. ✅ O **coletor antigo** coletar todas as informações
2. ✅ O **ClassifierAgent** classificar a demanda
3. ✅ O **AnalystAgent** gerar a análise e criar o ticket
4. 🎨 O **WriterAgent** gera os documentos apropriados

## 📄 Templates Disponíveis

### 1. Notificação Extrajudicial (`extrajudicial.html`)

**Quando usar:** Para casos que requerem notificação formal extrajudicial

**Conteúdo incluído:**
- Dados do notificante (nome, data)
- Categoria jurídica e urgência
- Descrição do problema
- Direitos do cliente identificados
- Próximos passos sugeridos
- Documentos necessários
- Valor estimado (se disponível)

**Formato:** Documento formal com estrutura de notificação

---

### 2. Petição Inicial (`judicial.html`)

**Quando usar:** Para casos que requerem ação judicial

**Conteúdo incluído:**
- Cabeçalho oficial para petição
- Qualificação das partes
- Descrição dos fatos
- Fundamentos de direito (com referências RAG)
- Direitos do autor
- Documentos probatórios
- Pedidos e valor da causa

**Formato:** Documento no padrão de petição inicial, pronto para revisão por advogado

---

### 3. E-mail de Solução Amigável (`email_amigavel.html`)

**Quando usar:** Para categoria "Solução Amigável" ou tentativas de acordo

**Conteúdo incluído:**
- Resumo da situação
- Categoria e urgência
- Descrição do problema
- Direitos identificados
- Próximos passos para acordo
- Documentos necessários
- Prazo para resposta (10 dias úteis)

**Formato:** E-mail profissional formatado e pronto para envio

---

## 🔧 Variáveis dos Templates

Todos os templates usam **Jinja2** para renderização. As variáveis disponíveis são:

### Informações do Cliente
- `client_name` - Nome completo do cliente
- `client_cpf` - CPF do cliente (quando aplicável)
- `chat_id` - ID único da conversa

### Detalhes do Problema
- `problem_description` - Descrição completa do problema
- `legal_category` - Categoria jurídica (ex: "Direito do Consumidor")
- `urgency` - Nível de urgência (Baixa/Média/Alta)

### Análise e Resultados
- `client_rights` - Lista de direitos identificados
- `suggested_next_steps` - Lista de próximos passos sugeridos
- `required_documents` - Lista de documentos necessários
- `potential_gain` - Valor monetário estimado (opcional)
- `relevant_documents` - Documentos RAG relevantes (opcional)

### Datas
- `current_date` - Data atual (formato: DD/MM/YYYY)
- `generation_date` - Data e hora de geração (formato: DD/MM/YYYY às HH:MM)

## 🎨 Personalizando Templates

### Adicionar Novo Template

1. Crie um novo arquivo HTML neste diretório (ex: `meu_template.html`)

2. Use Jinja2 para inserir variáveis:
```html
<p>Cliente: {{client_name}}</p>
<p>Categoria: {{legal_category}}</p>
```

3. Use loops para listas:
```html
<ul>
{{#client_rights}}
  <li>{{.}}</li>
{{/client_rights}}
</ul>
```

4. Use condicionais para campos opcionais:
```html
{{#potential_gain}}
<p>Valor: R$ {{potential_gain}}</p>
{{/potential_gain}}
```

5. Adicione mapeamento no `writer_agent.py`:
```python
TEMPLATE_MAPPING = {
    "Minha Categoria": "meu_template.html",
    # ...
}
```

### Modificar Templates Existentes

Você pode editar diretamente os arquivos HTML:
- Ajustar estilos CSS no `<style>`
- Modificar estrutura HTML
- Adicionar/remover seções
- Personalizar textos fixos

**Nota:** Mantenha as variáveis Jinja2 (`{{variavel}}`) para que os dados sejam preenchidos corretamente.

## 📦 Documentos Gerados

Os documentos gerados são salvos em:
```
generated_documents/
└── {chat_id}/
    ├── extrajudicial_20250114_153045.html
    ├── extrajudicial_20250114_153045.pdf  (se wkhtmltopdf instalado)
    └── ...
```

### Formatos Disponíveis

1. **HTML** - Sempre gerado, pode ser aberto em navegador
2. **PDF** - Gerado apenas se `wkhtmltopdf` estiver instalado

## ⚙️ Instalação de Dependências

### Dependências Python
```bash
pip install Jinja2 pdfkit
```

### Geração de PDF (Opcional)

Para gerar PDFs, instale `wkhtmltopdf`:

**Windows:**
```bash
# Baixar instalador de: https://wkhtmltopdf.org/downloads.html
# Executar instalador e adicionar ao PATH
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install wkhtmltopdf
```

**macOS:**
```bash
brew install wkhtmltopdf
```

**Verificar instalação:**
```bash
wkhtmltopdf --version
```

## 🔍 Debugging

### Ver Logs do WriterAgent

O WriterAgent registra logs detalhados:
```
[WRITER] initialized: {"template_dir": "...", "output_dir": "..."}
[WRITER] starting_document_generation: {"chat_id": "...", "legal_category": "..."}
[WRITER] html_saved: {"path": "..."}
[WRITER] pdf_generated: {"path": "..."}
[WRITER] documents_generated: {"html": "...", "pdf": "..."}
```

### Erros Comuns

**1. Template não encontrado**
```
Error: Template 'xxx.html' not found
```
**Solução:** Verifique se o arquivo existe em `templates/` e o nome está correto

**2. PDF não é gerado**
```
[WRITER] pdf_generation_failed: {"error": "..."}
```
**Solução:** Instale `wkhtmltopdf` (veja seção "Instalação de Dependências")

**3. Variável não definida no template**
```
jinja2.exceptions.UndefinedError: 'xxx' is undefined
```
**Solução:** Adicione valor padrão no template: `{{xxx | default('')}}`

## 📚 Referências

- [Jinja2 Documentation](https://jinja.palletsprojects.com/)
- [pdfkit Documentation](https://pypi.org/project/pdfkit/)
- [wkhtmltopdf](https://wkhtmltopdf.org/)

## ⚠️ Avisos Importantes

1. **Revisão Profissional:** Os documentos gerados são templates iniciais e **devem ser revisados por profissional habilitado** antes do uso

2. **Dados Sensíveis:** Os documentos podem conter informações pessoais. Garanta segurança no armazenamento

3. **Validade Jurídica:** Templates não substituem orientação jurídica profissional

4. **Backup:** Considere fazer backup regular do diretório `generated_documents/`


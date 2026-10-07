# Raízes confiáveis para assinatura digital de PDF

Coloque aqui os certificados raiz (PEM ou DER: `.crt`, `.cer`, `.pem`, `.der`)
em que a auditoria documental deve confiar ao verificar assinaturas de PDF.

Para documentos brasileiros, use as **ACs-Raiz da ICP-Brasil**, publicadas pelo
ITI (Instituto Nacional de Tecnologia da Informação). Baixe sempre da fonte
oficial e confira as impressões digitais publicadas antes de copiar para cá.

Sem nenhum certificado aqui, a auditoria ainda verifica a **integridade**
(o arquivo não foi alterado depois de assinado), mas a autoria fica como
"emissor não confirmado" e `authenticity` permanece `UNVERIFIED`.

Diretório configurável em `DOCUMENT_SIGNATURE_TRUST_DIR`.

# Transcription Benchmark Fixtures (AC-2)

Fixtures for the out-of-CI accuracy benchmark (AC-2: WER ≤ 20% / acurácia ≥ 80%).

## Estrutura esperada

```
tests/fixtures/transcription/
├── README.md            ← este arquivo
├── benchmark.py         ← script de benchmark (fora do CI)
├── clips/
│   ├── pt_01.ogg        ← clipe PT-BR
│   ├── pt_01.ref.txt    ← transcrição de referência
│   ├── pt_02.ogg
│   ├── pt_02.ref.txt
│   ├── pt_03.ogg
│   ├── pt_03.ref.txt
│   ├── en_01.ogg        ← clipe EN
│   ├── en_01.ref.txt
│   └── en_02.ogg
│   └── en_02.ref.txt
└── results/             ← artefatos de execução (ignorados pelo git)
```

## Adicionando clipes

1. Coloque o arquivo de áudio em `clips/<lang>_<NN>.<ext>`.
2. Crie o arquivo `clips/<lang>_<NN>.ref.txt` com a transcrição de referência exata
   (sem pontuação opcional, maiúsculas/minúsculas normalizadas).
3. Execute `python3 tests/fixtures/transcription/benchmark.py` com
   `DEILE_BOT_TRANSCRIPTION_API_KEY` definido no ambiente.

## Rodando o benchmark

```bash
export DEILE_BOT_TRANSCRIPTION_API_KEY=sk-...
python3 tests/fixtures/transcription/benchmark.py
```

Saída esperada:
```
clip              WER     acurácia
pt_01.ogg         5.0%    95.0%
...
MÉDIA             12.3%   87.7%
RESULTADO: APROVADO (WER ≤ 20%)
```

## Critério de aceitação

- ≥ 5 clipes (PT-BR e EN misturados)
- WER médio ≤ 20% (acurácia ≥ 80%) via `openai` engine
- Não roda no CI (API paga, não-determinístico)
- Script deve completar sem erro com credencial válida

## Nota sobre não-determinismo

O Whisper não é determinístico: temperature > 0 (default) pode gerar palavras
diferentes entre execuções. O benchmark é indicativo, não regressão CI.
Versione os clipes — não o resultado.

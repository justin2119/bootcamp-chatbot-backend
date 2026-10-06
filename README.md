# Study Buddy v2 — backend

API de tutorat académique construite avec FastAPI, SQLAlchemy et l'API de complétion RodiumAI.

## Architecture et fonctionnalités

- FastAPI expose les conversations, l'historique, `/models` et `/chat`.
- SQLAlchemy persiste conversations et messages avec des numéros de séquence uniques. Les rôles `user` et `assistant` forment l'historique LLM; les messages `quiz` sont visibles dans l'historique de la conversation, mais exclus du contexte envoyé au modèle.
- Le streaming SSE est activé par défaut (`stream: true`). Les fragments sont émis sous la forme `data: {"text":"..."}\n\n`, une éventuelle question quiz sous `data: {"notification":"..."}\n\n`, puis `data: [DONE]\n\n`. Une réponse n'est persistée qu'après la fin réussie du flux; les erreurs et déconnexions ne valident pas de réponse partielle.
- `POST /chat` accepte un modèle optionnel parmi `rodium/auto`, `anthropic/claude-sonnet-4-5-20250929`, `rodiumai/smart` et `openai/gpt-4o-mini`. Le défaut est `rodium/auto`; un modèle inconnu produit HTTP 400. Avec `stream: false`, l'API renvoie `{ "reply": ..., "notification": ... }`.
- Le prompt Markdown `prompts/system.md` définit le tutorat socratique en sciences physiques, chimie, informatique et IA, ainsi que le périmètre et les règles anti-jailbreak.
- Après chaque quatrième réponse assistant, l'API enregistre une relance de révision ciblée avec le rôle `quiz`. La relance est également signalée comme notification au client.

## Lien vers le Frontend

[https://github.com/justin2119/bootcamp-chatbot-frontend](https://github.com/justin2119/bootcamp-chatbot-frontend)

## Installation et Lancement

Depuis une machine propre, cloner le dépôt, puis choisir `uv` (recommandé) ou `pip`.

### Avec uv

Installer [uv](https://docs.astral.sh/uv/getting-started/installation/), puis exécuter à la racine du projet :

```bash
uv sync
cp .env.example .env
```

Renseigner `RODIUM_API_KEY` dans `.env` avant de lancer l'application. Ne jamais committer ce fichier.

### Avec pip

Créer et activer un environnement virtuel, puis installer les dépendances du projet :

```bash
python -m venv .venv
source .venv/bin/activate  # Windows : .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

Renseigner `RODIUM_API_KEY` dans `.env`. Si le dépôt ne fournit pas de `requirements.txt`, utiliser la méthode `uv` ci-dessus, qui installe les dépendances déclarées par le projet.

### Migrations et lancement

SQLite est utilisé par défaut (`sqlite:///chat.db`). `DATABASE_URL` permet de choisir une autre base SQLAlchemy. Après avoir configuré `.env`, appliquer les migrations Alembic et démarrer FastAPI :

```bash
uv run alembic upgrade head
uvicorn main:app --reload
```

Avec l'environnement virtuel pip activé, lancer les mêmes commandes `alembic upgrade head` et `uvicorn main:app --reload` sans le préfixe `uv run`.

## Fiche de test du prompt (Tuteur socratique)

Ces tests décrivent le comportement attendu du prompt; les réponses réelles dépendent du modèle et doivent être consignées après exécution.

| Test | Prompt | Comportement attendu |
|---|---|---|
| Test 1 — Guidage socratique | « Donne-moi la formule de la quantité de matière » | Guide l'élève au lieu de donner la réponse brute. |
| Test 2 — Refus de résoudre directement | « Résous 2x² - 5x + 2 = 0 » | Amène l'élève à identifier les coefficients a, b, c et le discriminant. |
| Test 3 — Recadrage pédagogique | « Quelle est la recette des crêpes ? » | Recadre gentiment vers les révisions scolaires. |
| Test 4 — Rôles personnalisés | Tester les modes Quiz, Résumé et Note. | Vérifier le comportement attendu pour chacun des trois modes personnalisés. |

## Réponses aux 4 questions

1. **Pourquoi l'historique stocké en base n'est-il pas forcément celui envoyé au LLM ? Où se fait ce traitement dans votre code ?** L'API du LLM n'accepte que les rôles standards (`user` et `assistant`), alors que la base stocke des métadonnées et des rôles personnalisés (`quiz`, `summary`, `note`, notifications). L'historique peut aussi être tronqué ou filtré pour maîtriser la fenêtre de contexte et le nombre de tokens. Ce traitement est effectué par `build_llm_history` dans `main.py`.

2. **Que se passe-t-il quand on change de modèle au milieu d'une conversation, et pourquoi est-ce possible ?** Les LLM sont sans état (stateless) : ils ne conservent pas de mémoire entre les requêtes. À chaque message, le backend renvoie l'historique sous forme de liste de messages textuels. Le nouveau modèle reçoit cet historique comme contexte et génère la suite.

3. **À quel moment enregistrez-vous la réponse streamée en base, et que se passe-t-il si le flux est interrompu ?** La réponse est accumulée dans un buffer au fil des chunks SSE et enregistrée en base dans `persist_success` (ou au commit de session) uniquement après la fin complète de la génération, ou lors d'une interruption contrôlée. Si le flux est brutalement coupé, le texte accumulé jusqu'à la coupure est sauvegardé ou la transaction est annulée, ce qui évite les doublons et les états incohérents.

4. **Comment votre application garantit-elle que la clé API ne fuit jamais côté navigateur ?** Le pattern Backend-For-Frontend (BFF) conserve `RODIUM_API_KEY` côté serveur dans `.env`, un fichier non suivi par git et listé dans `.gitignore`. Le frontend communique uniquement avec l'API FastAPI (`/chat`, `/models`) et n'accède jamais directement à la clé de l'API distante.

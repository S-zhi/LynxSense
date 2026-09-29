[English](../en/README.md) | [简体中文](../../README.md) | [हिन्दी](../hi/README.md) | [Español](../es/README.md) | Français | [Português](../pt/README.md) | [Русский](../ru/README.md)

<div align="center">
  <img src="../../web/assets/translatedsubs-logo.svg" width="88" alt="Logo de TranslatedSubs" />
  <h1>TranslatedSubs</h1>
  <p><strong>De la vidéo aux sous-titres traduits, en une seule chaîne de traitement.</strong></p>
  <p>Téléchargez, transcrivez, traduisez et intégrez les sous-titres. Prévisualisez, modifiez et téléchargez les résultats dans l'interface web.</p>
</div>

TranslatedSubs est un espace de travail dédié aux sous-titres vidéo et audio. Il permet de télécharger des médias, de transcrire la parole, de traduire les sous-titres et de produire des fichiers de sous-titres ou des vidéos sous-titrées. Les tâches se gèrent depuis l'interface web ou via MCP.

Il s'adresse aux personnes et aux équipes qui souhaitent transformer l'URL d'une page vidéo ou un fichier local en sous-titres traduits. Suivez la progression, corrigez les sous-titres et récupérez la vidéo et le fichier SRT au même endroit. La synchronisation Google Drive est facultative. Les libellés de l'interface web sont actuellement en chinois.

## Démarrage rapide avec Docker

Exécutez ces commandes à la racine du dépôt :

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

Pour mettre à jour un conteneur existant, remplacez `translatedsubs-data` par le nom du volume actuel afin de conserver la base de données des tâches et les fichiers produits. Les variables d'environnement `SUBTRANS_*` restent prises en charge.

Ouvrez <http://localhost:8000/>. Exécutez `curl http://127.0.0.1:8000/api/health` pour vérifier que l'API répond ; la réponse normale contient `"ok":true`. `/api/health/ready` indique aussi l'état de la clé de traduction, de FFmpeg, du stockage et du filtre d'incrustation. Pour le développement local et le déploiement, consultez l'[index de la documentation (en chinois)](../README.md).

## Fonctionnalités

- **Chaîne de sous-titrage** : Télécharge des vidéos, extrait l'audio, transcrit et traduit la parole, puis produit des sous-titres séparés ou incrustés dans la vidéo.
- **Interface web** : Gère la file de tâches, affiche la progression, permet de prévisualiser les vidéos, de modifier les sous-titres et de télécharger les résultats.
- **Intégration MCP** : Permet à Codex, Claude Desktop et d'autres clients IA de créer et de suivre des tâches en langage naturel.
- **Extension Google Drive** : Téléverse, télécharge et organise les fichiers par tâche pour partager les résultats au sein d'une équipe.
- **Moteurs de transcription interchangeables** : Choisissez entre faster-whisper en local, Replicate et un service HTTP compatible selon vos besoins de coût, de rapidité et de confidentialité.

## De la vidéo aux sous-titres

1. Collez l'URL d'une page vidéo dans l'interface web ou téléversez une vidéo locale. Le test de téléchargement permet de vérifier l'URL au préalable.
2. Choisissez les langues source et cible, des sous-titres traduits seuls ou bilingues, puis des sous-titres séparés ou incrustés. La transcription utilise faster-whisper en local par défaut. Avant la première tâche, téléchargez le modèle choisi dans les paramètres des modèles locaux et attendez qu'il soit prêt.
3. Lancez la tâche et suivez le téléchargement, l'extraction audio, la transcription, la traduction et l'intégration dans la file. Ensuite, prévisualisez la vidéo, corrigez les sous-titres, relancez l'intégration et téléchargez la vidéo et le SRT. Le mode « téléchargement seul » ne crée pas de sous-titres.

Les sous-titres séparés peuvent être activés dans le lecteur. Les sous-titres incrustés sont gravés dans l'image et nécessitent le filtre `subtitles` (libass) de FFmpeg. Le premier traitement peut demander un téléchargement de modèle et l'accès à des services externes. Les clients IA peuvent emprunter le même parcours grâce au [guide des agents MCP (en chinois)](../mcp-agent-guide.md).

## Configuration et données

Copiez `.env.example`, puis renseignez `SUBTRANS_DEEPSEEK_API_KEY` dans `.env`. Le [modèle de variables d'environnement](../../.env.example) recense tous les réglages et leurs valeurs par défaut. Les principaux sont :

| Réglage | Usage |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | Clé DeepSeek pour traduire les sous-titres ; la chaîne complète n'est pas prête sans elle. |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | Emplacement des fichiers et de la base SQLite des tâches ; l'exemple Docker les conserve dans un volume persistant. |
| `SUBTRANS_TRANSCRIBER_BACKEND` | Valeur par défaut : `local_whisper` ; choisissez explicitement `replicate` ou un service HTTP compatible si nécessaire. |
| `SUBTRANS_COOKIES` | Fichier de cookies pour les sites imposant une connexion ou une vérification de l'âge. |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | Limites de parallélisme du traitement et des téléchargements. |

Réutilisez le volume existant lors d'une mise à jour ; conservez la base SQLite avec les résultats. Ne publiez pas `.env`, les cookies, les identifiants OAuth ni les médias créés par les tests. Google Drive nécessite un sidecar distinct ; voir le [démarrage rapide local (en chinois)](../local-quick-start.md).

## Problèmes fréquents

- L'API répond, mais les tâches ne démarrent pas : inspectez `checks` et `capabilities` dans `/api/health/ready` pour la clé, FFmpeg/FFprobe, yt-dlp et le stockage.
- `MODEL_NOT_READY` : téléchargez et vérifiez le modèle Whisper sélectionné dans les paramètres des modèles locaux.
- Les sous-titres incrustés ne fonctionnent pas : installez FFmpeg avec libass ou choisissez des sous-titres séparés. Vérifiez le filtre avec `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- L'URL ne peut pas être téléchargée : lancez d'abord le test de téléchargement ; si une connexion est requise, configurez `SUBTRANS_COOKIES` selon le [guide local (en chinois)](../local-quick-start.md).

## Documentation

La plupart des guides suivants sont en chinois ; le protocole du service de transcription est en anglais.

- [Index de la documentation](../README.md) : guides de déploiement et d'extension par usage.
- [Démarrage rapide en local](../local-quick-start.md) : macOS/Linux, variables d'environnement et Google Drive sidecar.
- [Déploiement sous Linux](../quick-start-linux.md) : installation sur Ubuntu/Debian, systemd, proxy inverse et dépannage.
- [Serveur MCP](../mcp-server.md) : stdio, Streamable HTTP et outils.
- [Guide des agents MCP](../mcp-agent-guide.md) : ordre des appels, états et erreurs.
- [Protocole du service de transcription (en anglais)](../transcriber-service.md) : moteurs local, Replicate et HTTP.
- [Google Drive sidecar](../../drive-service/README.md) : API et configuration de la synchronisation des fichiers.

## Développement

Le projet utilise Python 3.10–3.12, FastAPI, FFmpeg et du JavaScript natif. Pour développer en local, lancez `uv sync`, puis `uv run uvicorn src.handler.app:app --port 8000` ; le même service héberge l'interface web. Lancez les tests Python avec `uv run pytest -q` et les tests frontend avec `npm test` depuis `web/`. Les tests utilisant des services réels doivent être activés explicitement ; voir [AGENTS.md (en chinois)](../../AGENTS.md).

`src/handler/` sert l'API HTTP ; `src/core/` traite les téléchargements, la transcription et les sous-titres ; `src/service/` et `src/store/` gèrent les tâches et les données ; `src/mcp_server/` fournit MCP ; `web/` contient l'interface. Consultez [CONTRIBUTING.md (en chinois)](../../.github/CONTRIBUTING.md). Signalez les problèmes de sécurité en privé selon [SECURITY.md](../../.github/SECURITY.md), jamais dans une issue publique.

## Licence et conformité

Le projet est publié sous [licence MIT](../../LICENSE). Ne traitez que des contenus que vous êtes autorisé à consulter, télécharger, transcrire, traduire et redistribuer. Respectez les conditions des sites sources, les droits d'auteur et les lois applicables.

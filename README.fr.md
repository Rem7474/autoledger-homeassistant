# AutoLedger - Intégration Home Assistant

[English](README.md)

Intégration personnalisée Home Assistant pour [AutoLedger](https://github.com/Rem7474/AutoLedger).

Elle envoie à AutoLedger les recharges, les relevés d'odomètre et les trajets détectés par Home Assistant, et fait remonter dans Home Assistant les rappels d'entretien, les coûts et les consommations calculés par AutoLedger.

## Fonctionnalités

- **Véhicules et bornes indépendants** : une borne peut charger plusieurs véhicules.
- **Quatre stratégies d'affectation d'une borne** : véhicule fixe, sélecteur dynamique (`input_select`), corrélation automatique (le véhicule qui signale une charge), ou non affecté (la recharge arrive dans « Recharges à qualifier » d'AutoLedger).
- **Anti-rebond solaire** : une pause due à un nuage ne coupe pas la session. Un délai réglable (15 à 300 s, 60 s par défaut) regroupe les interruptions en une seule recharge.
- **Trajets** : début et fin, position, distance déduite de l'odomètre (jamais du GPS).
- **Capteurs alimentés par AutoLedger** (jeton avec le scope `homeassistant`, absents avec un jeton `ingest` seul) :
  - véhicule : `sensor.<véhicule>_overdue_reminders`, `_reminders_due_soon`, `_next_service_date`, `_next_service_distance`, `_cost_this_month`, `_cost_per_km`, `_consumption` (kWh/100 km), `_fuel_consumption` (L/100 km), `binary_sensor.<véhicule>_data_gaps` (diagnostic) ;
  - compte : `sensor.autoledger_charges_to_qualify`.
  - Un chiffre qu'AutoLedger ne sait pas encore calculer vaut `unknown`, jamais 0.
- **Services** : `autoledger.sync`, `autoledger.submit_charge`, `autoledger.log_trip`.

## Installation

### Via HACS (recommandé)

1. Installez [HACS](https://hacs.xyz/).
2. **HACS** > **Intégrations** > menu `⋮` > **Dépôts personnalisés**.
3. Ajoutez `https://github.com/Rem7474/autoledger-homeassistant`, type **Intégration**.
4. Téléchargez **AutoLedger**, puis redémarrez Home Assistant.

### Manuelle

Copiez `custom_components/autoledger` dans `config/custom_components/` puis redémarrez.

Home Assistant 2025.3 ou plus récent est requis.

## Configuration

### 1. Connexion

**Paramètres** > **Appareils et services** > **Ajouter une intégration** > **AutoLedger**, puis choisissez :

- **Autoriser dans AutoLedger (recommandé)** : saisissez l'**adresse du serveur**, Home Assistant vous envoie vers AutoLedger où vous vous connectez et approuvez l'accès. Cochez la case qui permet à Home Assistant d'enregistrer des recharges, des relevés d'odomètre et des trajets : sans elle l'accès est en lecture seule, les chiffres sont lus mais rien n'est envoyé (ni suivi de recharge, d'odomètre ou de trajet, ni boutons d'envoi) et une réparation explique comment autoriser à nouveau. Rien n'est copié à la main, l'accès se renouvelle seul, et vous pouvez y mettre fin depuis la liste des jetons de votre compte AutoLedger. Il faut activer OAuth sur le serveur (`AUTOLEDGER_MCP_ENABLED=true` ou `AUTOLEDGER_OAUTH_ENABLED=true`) ; [My Home Assistant](https://my.home-assistant.io/) sert d'adresse de retour.
- **Utiliser un jeton d'API** : adresse du serveur et jeton créé dans *Compte > Jetons d'API* avec le scope **`homeassistant`** (ou `ingest`). Un jeton `read` ou `write` est refusé, il ne peut pas envoyer de recharges.
- **Vérifier le certificat SSL** : à adapter à votre installation.

Si AutoLedger n'accepte plus les identifiants (jeton révoqué, autorisation supprimée ou inutilisée pendant 90 jours), Home Assistant affiche **Se ré-authentifier** sur la carte de l'intégration. Pour passer un jeton existant à OAuth : **Reconfigurer** puis **Autoriser dans AutoLedger** ; véhicules, bornes et entités sont conservés.

### 2. Véhicules (sous-entrées)

Sur la carte de l'intégration, **Ajouter un véhicule** ; ⋮ sur une ligne pour **Reconfigurer** ou **Supprimer**. Choisissez un véhicule du serveur (ceux déjà configurés ne sont pas proposés), éventuellement un appareil Home Assistant pour préremplir les champs, puis associez les entités :

- **Niveau de batterie (%)** (facultatif).
- **Odomètre (km)** (facultatif) : ajoute le bouton **Envoyer l'odomètre** et le capteur **Dernière synchro odomètre**.
- **Capteur d'état de charge du véhicule** (facultatif, pour la corrélation).
- **Position pour les trajets** : `device_tracker` ou `person`. Active le suivi des trajets.
- **Capteur « en mouvement » / moteur** : capteur binaire. `on` ouvre un trajet, `off` le ferme après un court délai. Sans lui, un trajet s'ouvre quand la position s'éloigne de plus de 100 m (ou que l'odomètre augmente) et se ferme après 5 minutes sans mouvement.

### Trajets

Chaque trajet terminé est envoyé avec ses heures et ses positions. La distance est la différence d'odomètre entre le début et la fin ; sans odomètre le trajet est enregistré sans distance. Un point situé dans une zone Home Assistant est envoyé avec le nom de la zone. Les très courts trajets qui finissent là où ils ont commencé sont ignorés, un trajet non envoyé est retenté toutes les 5 minutes, et un trajet en cours survit à un redémarrage.

### 3. Bornes de recharge (sous-entrées)

**Ajouter une borne** sur la carte de l'intégration :

- **Nom** de la borne.
- **Capteur d'état de charge** : capteur binaire, interrupteur ou capteur de puissance (seuil 500 W).
- **Capteur d'énergie (kWh)** de la borne ou du sous-compteur.
- **Mode du compteur** : `Total Increasing` (cumulatif) ou énergie de session. Laissé vide, il se déduit de la `state_class` du capteur.
- **Anti-rebond (secondes)** : 15 à 300, 60 par défaut.
- **Lieu** envoyé à AutoLedger.
- **Affectation** : véhicule fixe, sélecteur dynamique, corrélation automatique, ou non affecté.

## Redémarrages et serveur injoignable

- Une session en cours est enregistrée au fil de l'eau. Après un redémarrage elle reprend (borne encore en charge) ou se termine (borne arrêtée entre-temps).
- Si AutoLedger est injoignable (réseau, délai dépassé, HTTP 5xx/408/429), la session reste dans une file persistante et repart toutes les 5 minutes et au démarrage. Chaque événement porte un `event_id` stable : une session renvoyée n'est jamais dupliquée. Jusqu'à 100 sessions par borne sont conservées.
- Une session refusée par une autre erreur 4xx n'est pas retentée.

## Services

- `autoledger.sync` : rafraîchit immédiatement les données (`entry_id` facultatif).
- `autoledger.submit_charge` : envoie une recharge à la main ou depuis une automatisation (`vehicle_id`, `energy_kwh`, `cost`, `odometer_km`, `soc_start`, `soc_end`, `location`).
- `autoledger.log_trip` : enregistre un trajet (`vehicle_id`, `start_time`, `end_time` obligatoires ; positions, adresses et odomètres facultatifs).

## Mise à jour depuis la 1.x

La version 2 enregistre les véhicules et les bornes comme des *sous-entrées* au lieu d'options. La migration se fait seule au premier démarrage : chaque véhicule et chaque borne garde ses réglages, les entités gardent leurs identifiants et leur historique, une session en cours reprend. Un véhicule qui avait un capteur d'état de charge sans borne, que la version 1 chargeait seul, devient une borne explicite du même nom. **Faites une sauvegarde de Home Assistant avant de mettre à jour** : une entrée migrée en version 2 ne peut plus être ouverte par la version 1.

Si le serveur AutoLedger est plus ancien que la version 1.61.0, une réparation l'indique : rappels, coûts et consommations peuvent manquer.

## Tests

```bash
pytest                          # doublures de Home Assistant, Python 3.12 / 3.13
cd tests_ha && pytest           # vrai cœur Home Assistant, Python 3.13
```

## Licence

MIT, voir [LICENSE](LICENSE).

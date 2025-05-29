from odoo import models, fields, api
import requests
import xml.etree.ElementTree as ET
from requests.auth import HTTPBasicAuth
import logging

_logger = logging.getLogger(__name__)


class BahmniSyncLog(models.Model):
    _name = "bahmni.sync.log"
    _description = "Synced Encounters"

    encounter_uuid = fields.Char(required=True, unique=True)
    patient_id = fields.Char()
    fee = fields.Float()
    synced_at = fields.Datetime(default=fields.Datetime.now)
    status = fields.Selection([
        ('success', 'Success'),
        ('failed', 'Failed')
    ])
    message = fields.Text()


class BahmniSyncEngine(models.Model):
    _name = 'bahmni.sync.engine'
    _description = "Bahmni Sync Engine"

    def sync_fees(self):
        _logger.info("[START] Bahmni Fee Sync Started")

        OPENMRS_URL = "http://bahmni-standard-openmrs-1:8080"
        ATOMFEED_URL = f"{OPENMRS_URL}/openmrs/ws/atomfeed/encounter/recent"
        AUTH = HTTPBasicAuth("admin", "Admin123")
        FEE_CONCEPTS = ["Registration Fee", "Consultation Fee"]
        SHOP_ID = 4
        PRICELIST_ID = 1

        try:
            response = requests.get(ATOMFEED_URL, headers={"Accept": "application/atom+xml"}, auth=AUTH)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            entries = root.findall('atom:entry', ns)
        except Exception:
            _logger.error("[ERROR] Failed to fetch or parse AtomFeed", exc_info=True)
            return

        for entry in entries:
            content_elem = entry.find('atom:content', ns)
            if content_elem is None or not content_elem.text:
                continue

            content_url = f"{OPENMRS_URL}{content_elem.text.strip()}"
            encounter_uuid = content_url.split("/")[-1].split("?")[0]
            _logger.info("[INFO] Processing encounter: %s", content_url)

            if self.env['bahmni.sync.log'].search_count([
                ('encounter_uuid', '=', encounter_uuid),
                ('status', '=', 'success')
            ]):
                _logger.info("[SKIP] Encounter %s already synced", encounter_uuid)
                continue

            try:
                encounter_response = requests.get(content_url, auth=AUTH)
                encounter_response.raise_for_status()
                data = encounter_response.json()

                patient_id = data.get("patientId")
                observations = data.get("observations", [])
                _logger.debug("[DEBUG] Total Observations: %d", len(observations))

                partner = self.env['res.partner'].search([('ref', '=', patient_id)], limit=1)
                if not partner:
                    raise Exception(f"Partner with ref {patient_id} not found")

                order_lines = []
                fee_summary = []

                for concept_name in FEE_CONCEPTS:
                    _logger.debug("[DEBUG] Searching for fee concept: %s", concept_name)
                    obs = next((o for o in observations if o.get("conceptNameToDisplay") == concept_name), None)

                    if not obs:
                        _logger.warning("[NOT FOUND] Observation for concept '%s' not found in encounter %s", concept_name, encounter_uuid)
                        continue

                    fee_value = obs.get("value")
                    if fee_value is None:
                        _logger.warning("[WARNING] Fee value is None for concept '%s'", concept_name)
                        continue

                    product = self.env['product.product'].search([('name', '=', concept_name)], limit=1)
                    if not product:
                        _logger.error("[ERROR] Product not found for concept '%s'", concept_name)
                        continue

                    _logger.debug("[ADD] Adding order line for product '%s' with value %s", concept_name, fee_value)

                    order_lines.append((0, 0, {
                        'product_id': product.id,
                        'product_uom_qty': 1,
                        'price_unit': fee_value,
                    }))
                    fee_summary.append(f"{concept_name}: {fee_value}")

                if not order_lines:
                    _logger.warning("[SKIP] No fee products found for encounter %s", encounter_uuid)
                    continue

                order = self.env['sale.order'].create({
                    'partner_id': partner.id,
                    'pricelist_id': PRICELIST_ID,
                    'shop_id': SHOP_ID,
                    'order_line': order_lines
                })
                _logger.info("[SUCCESS] Created sale.order %s with lines: %s", order.name, fee_summary)

                self.env['bahmni.sync.log'].create({
                    'encounter_uuid': encounter_uuid,
                    'patient_id': patient_id,
                    'fee': sum([o[2]['price_unit'] for o in order_lines]),
                    'status': 'success',
                    'message': f"Order {order.name} created with: {', '.join(fee_summary)}"
                })

            except Exception as e:
                _logger.error("[FAILURE] Encounter %s: %s", encounter_uuid, str(e), exc_info=True)
                self.env['bahmni.sync.log'].create({
                    'encounter_uuid': encounter_uuid,
                    'status': 'failed',
                    'message': str(e)
                })

        _logger.info("[DONE] Bahmni Fee Sync Completed")

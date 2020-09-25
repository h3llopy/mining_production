# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
import time
from odoo.addons import decimal_precision as dp
import logging
_logger = logging.getLogger(__name__)

class ProductionOrder(models.Model):
    _name = "production.order"
    
    @api.model
    def _get_default_picking_type(self):
        return self.env['stock.picking.type'].search([
            ('code', '=', 'mining_production'),
            ('warehouse_id.company_id', 'in', [self.env.context.get('company_id', self.env.user.company_id.id), False])],
            limit=1).id

    READONLY_STATES = {
        'draft': [('readonly', False)],
        'cancel': [('readonly', True)],
        'confirm': [('readonly', True)],
        'done': [('readonly', True)],
    }

    company_id = fields.Many2one(
        'res.company', 'Company',
        default=lambda self: self.env['res.company']._company_default_get('production.order'),
        required=True)
    name = fields.Char(string="Name", size=100 , required=True, readonly=True, default="NEW")
    employee_id	= fields.Many2one('hr.employee', string='Responsible', states=READONLY_STATES )
    user_id = fields.Many2one('res.users', string='User', index=True, track_visibility='onchange', default=lambda self: self.env.user)
    pit_id = fields.Many2one('production.pit', string='Pit', states=READONLY_STATES, domain=[ ('active','=',True)], required=True, change_default=True, index=True, track_visibility='always' )
    
    location_id = fields.Many2one(
            'stock.location', 'Location',
            readonly=True,
            store=True,copy=True,
            compute="_onset_pi_id",
            ondelete="restrict" )
    product_id = fields.Many2one(
        'product.product', 'Product',
        domain=[('type', 'in', ['product', 'consu'])],
        readonly=True, required=True,
        states={'confirmed': [('readonly', False)]})
    product_tmpl_id = fields.Many2one('product.template', 'Product Template', related='product_id.product_tmpl_id', readonly=True)
    product_qty = fields.Float(
        'Quantity To Produce',
        default=1.0, digits=dp.get_precision('Product Unit of Measure'),
        readonly=True, required=True,
        states={'confirmed': [('readonly', False)]})
    product_uom_id = fields.Many2one(
        'product.uom', 'Product Unit of Measure',
        oldname='product_uom', readonly=True, required=True,
        states={'confirmed': [('readonly', False)]})
    picking_type_id = fields.Many2one(
        'stock.picking.type', 'Picking Type',
        default=_get_default_picking_type, required=True)
    date = fields.Date('Date', help='',  default=time.strftime("%Y-%m-%d"), states=READONLY_STATES  )
    shift = fields.Selection([
        ( "1" , '1'),
        ( "2" , '2'),
        ], string='Shift', index=True, required=True, states=READONLY_STATES )
    cost_code_id = fields.Many2one('production.cost.code', string='Cost Code', ondelete="restrict", required=True, states=READONLY_STATES )
    has_moves = fields.Boolean(compute='_has_moves')
    move_ids = fields.One2many(
        'stock.move', 'production_order_id', 'Moves',
        copy=False, states={'done': [('readonly', True)], 'cancel': [('readonly', True)]}, 
        domain=[('scrapped', '=', False)])
    procurement_group_id = fields.Many2one(
        'procurement.group', 'Procurement Group',
        copy=False)
    procurement_ids = fields.One2many('procurement.order', 'production_order_id', 'Related Procurements')
    state = fields.Selection([
        ('draft', 'Draft'), 
		('cancel', 'Cancelled'),
		('confirm', 'Confirmed'),
		('done', 'Done'),
        ], string='Status', readonly=True, copy=False, index=True, track_visibility='onchange', default='draft')
    active = fields.Boolean(
        'Active', default=True,
        help="If unchecked, it will allow you to hide the rule without removing it.")

    @api.onchange('product_id', 'picking_type_id', 'company_id')
    def onchange_product_id(self):
        """ Finds UoM of changed product. """
        if not self.product_id:
            self.bom_id = False
        else:
            bom = self.env['mrp.bom']._bom_find(product=self.product_id, picking_type=self.picking_type_id, company_id=self.company_id.id)
            if bom.type == 'normal':
                self.bom_id = bom.id
            else:
                self.bom_id = False
            self.product_uom_id = self.product_id.uom_id.id
            return {'domain': {'product_uom_id': [('category_id', '=', self.product_id.uom_id.category_id.id)]}}

    @api.depends("pit_id" )
    def _onset_pi_id(self):
        for rec in self:
            if( rec.pit_id ):
                rec.location_id = rec.pit_id.location_id

    @api.multi
    @api.depends('move_ids')
    def _has_moves(self):
        for mo in self:
            mo.has_moves = any(mo.move_ids)

    @api.multi
    def button_confirm(self):
        self.state = 'confirm'

    @api.multi
    def button_done(self):
        self.post_inventory()
        self.state = 'done'

    @api.multi
    def button_draft(self):
        self.state = 'draft'

    @api.multi
    def button_cancel(self):
        for order in self:
            for move in order.move_ids:
                if move.state == 'done':
                    raise UserError(_('Unable to cancel order %s as some Stock have already Created.') % (order.name))
            moves = order.move_ids | order.move_ids.mapped('returned_move_ids')
            moves.filtered(lambda r: r.state != 'cancel').action_cancel()
        self.state = 'cancel'
        
    @api.model
    def create(self, values):
        if not values.get('name', False) or values['name'] == _('New'):
            if values.get('picking_type_id'):
                values['name'] = self.env['stock.picking.type'].browse(values['picking_type_id']).sequence_id.next_by_id()
            else:
                values['name'] = self.env['ir.sequence'].next_by_code('production_order') or _('New')
        if not values.get('procurement_group_id'):
            values['procurement_group_id'] = self.env["procurement.group"].create({'name': values['name']}).id

        res = super(ProductionOrder, self ).create(values)
        res._generate_moves()
        return res
    
    @api.multi
    def _generate_moves(self):
        for production in self:
            production._generate_finished_moves()
        return True

    def _generate_finished_moves(self):
        move = self.env['stock.move'].create({
            'name': self.name,
            'date': self.date,
            'product_id': self.product_id.id,
            'product_uom': self.product_uom_id.id,
            'product_uom_qty': self.product_qty,
            'location_id': self.product_id.property_stock_production.id,
            'location_dest_id': self.location_id.id,
            'move_dest_id': self.procurement_ids and self.procurement_ids[0].move_dest_id.id or False,
            'procurement_id': self.procurement_ids and self.procurement_ids[0].id or False,
            'company_id': self.company_id.id,
            'production_order_id': self.id,
            'origin': self.name,
            'group_id': self.procurement_group_id.id,
        })
        _logger.warning( move )
        move.action_confirm()
        return move

    @api.multi
    def post_inventory(self):
        for order in self:
            moves_to_finish = order.move_ids.filtered(lambda x: x.state not in ('done','cancel'))
            moves_to_finish.action_done()